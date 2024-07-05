import copy
import pickle

import numpy as np
from skimage import io
import skimage.transform as st
from sklearn.metrics import precision_score, accuracy_score, recall_score
import cv2
from ...ops.roiaware_pool3d import roiaware_pool3d_utils
from ...utils import box_utils, calibration_kitti, common_utils, object3d_kitti
from ..dataset import DatasetTemplate
from .kitti_utils import pad_image, get_fov_flag
from ...utils import debug_utils
from collections import defaultdict
from ...utils.calibration_kitti import TorchCalibration
from ...structures import Instances, Boxes
import torch
from torch.nn.functional import grid_sample
from pcdet.utils.box_utils import in_2d_box
from tqdm import tqdm
from pathlib import Path
import os


class KittiFusionDataset(DatasetTemplate):
    def __init__(
        self,
        dataset_cfg,
        class_names,
        training=True,
        root_path=None,
        split=None,
        logger=None,
    ):
        """
        Args:
            root_path:
            dataset_cfg:
            class_names:
            training:
            logger:
        """
        super().__init__(
            dataset_cfg=dataset_cfg,
            class_names=class_names,
            training=training,
            root_path=root_path,
            logger=logger,
        )
        self.split = self.dataset_cfg.DATA_SPLIT[self.mode]
        self.root_split_path = self.root_path / (
            "training" if self.split != "test" else "testing"
        )
        # split_dir = self.root_path / 'ImageSets' / ('train' + '.txt')

        if split is None:
            split_dir = self.root_path / "ImageSets" / (self.split + ".txt")
        else:
            split_dir = self.root_path / "ImageSets" / (split + ".txt")

        self.sample_id_list = (
            [x.strip() for x in open(split_dir).readlines()]
            if split_dir.exists()
            else None
        )

        self.kitti_infos = []
        if split is not None:
            self.include_kitti_data_demo()
        else:
            self.include_kitti_data(self.mode)
        self.fake_label = dataset_cfg.get("USE_FAKE_LABEL", False)
        # self.include_kitti_data('train')
        fake_box_path = dataset_cfg.get("FAKE_BOX_PATH", "")
        self.fake_boxes = None
        self.box_thresh = self.dataset_cfg.get("BOX_THRESH", 0.3)
        if len(fake_box_path) > 0 and self.training:
            self.update_fake_box(fake_box_path)
            logger.info("Using fake box: %s" % fake_box_path)

    def update_fake_box(self, fake_box):
        if isinstance(fake_box, str):
            with open(fake_box, "rb") as f:
                self.fake_boxes = pickle.load(f)
        else:
            self.fake_boxes = fake_box

    def include_kitti_data_demo(self):
        if self.logger is not None:
            self.logger.info("Loading KITTI dataset demo")
        kitti_infos = []

        info_path = self.root_path / "kitti_infos_trainval.pkl"

        with open(info_path, "rb") as f:
            infos = pickle.load(f)
        frame2dict = {v["image"]["image_idx"]: i for i, v in enumerate(infos)}
        for frame_id in self.sample_id_list:
            if frame_id in frame2dict:
                kitti_infos.append(infos[frame2dict[frame_id]])
        # sorted(kitti_infos,key=lambda x:['image']['image_idx'])
        # kitti_infos.extend(infos)

        self.kitti_infos.extend(kitti_infos)

    def include_kitti_data(self, mode):
        if self.logger is not None:
            self.logger.info("Loading KITTI dataset")
        kitti_infos = []

        for info_path in self.dataset_cfg.INFO_PATH[mode]:
            info_path = self.root_path / info_path
            if not info_path.exists():
                continue
            with open(info_path, "rb") as f:
                infos = pickle.load(f)
                kitti_infos.extend(infos)

        self.kitti_infos.extend(kitti_infos)

        if self.logger is not None:
            self.logger.info("Total samples for KITTI dataset: %d" % (len(kitti_infos)))

    def set_split(self, split):
        super().__init__(
            dataset_cfg=self.dataset_cfg,
            class_names=self.class_names,
            training=self.training,
            root_path=self.root_path,
            logger=self.logger,
        )
        self.split = split
        self.root_split_path = self.root_path / (
            "training" if self.split != "test" else "testing"
        )

        split_dir = self.root_path / "ImageSets" / (self.split + ".txt")
        self.sample_id_list = (
            [x.strip() for x in open(split_dir).readlines()]
            if split_dir.exists()
            else None
        )

    def get_lidar(self, idx):
        lidar_file = self.root_split_path / "velodyne" / ("%s.bin" % idx)
        assert lidar_file.exists()
        return np.fromfile(str(lidar_file), dtype=np.float32).reshape(-1, 4)

    def get_image_shape(self, idx):
        img_file = self.root_split_path / "image_2" / ("%s.png" % idx)
        assert img_file.exists(), img_file
        return np.array(io.imread(img_file).shape[:2], dtype=np.int32)

    def get_image(self, idx):
        img_file = self.root_split_path / "image_2" / ("%s.png" % idx)
        assert img_file.exists()
        img = io.imread(img_file)
        if img.shape[2] == 4:
            img = img[:, :, :-1]
        img = np.ascontiguousarray(img[:, :, ::-1])  # convert to BGR
        return img

    def get_label(self, idx):
        label_file = self.root_split_path / "label_2" / ("%s.txt" % idx)
        assert label_file.exists()
        return object3d_kitti.get_objects_from_label(label_file)

    def get_calib(self, idx):
        calib_file = self.root_split_path / "calib" / ("%s.txt" % idx)
        assert calib_file.exists()
        return calibration_kitti.Calibration(calib_file)

    def get_road_plane(self, idx):
        plane_file = self.root_split_path / "planes" / ("%s.txt" % idx)
        if not plane_file.exists():
            return None

        with open(plane_file, "r") as f:
            lines = f.readlines()
        lines = [float(i) for i in lines[3].split()]
        plane = np.asarray(lines)

        # Ensure normal is always facing up, this is in the rectified camera coordinate
        if plane[1] > 0:
            plane = -plane

        norm = np.linalg.norm(plane[0:3])
        plane = plane / norm
        return plane

    @staticmethod
    def get_fov_flag(pts_rect, img_shape, calib):
        """
        Args:
            pts_rect:
            img_shape:
            calib:

        Returns:

        """
        pts_img, pts_rect_depth = calib.rect_to_img(pts_rect)
        val_flag_1 = np.logical_and(pts_img[:, 0] >= 0, pts_img[:, 0] < img_shape[1])
        val_flag_2 = np.logical_and(pts_img[:, 1] >= 0, pts_img[:, 1] < img_shape[0])
        val_flag_merge = np.logical_and(val_flag_1, val_flag_2)
        pts_valid_flag = np.logical_and(val_flag_merge, pts_rect_depth >= 0)

        return pts_valid_flag

    def get_infos(
        self, num_workers=4, has_label=True, count_inside_pts=True, sample_id_list=None
    ):
        import concurrent.futures as futures

        preds = None
        # if self.split == 'train' and len(self.dataset_cfg.get('FAKE_LABEL','')) > 0:
        #     with open(self.dataset_cfg.get('FAKE_LABEL'), 'rb') as f:
        #         preds = pickle.load(f)
        #
        #     # with open(self.dataset_cfg.get('FAKE_LABEL')[:-4]+'_lite.pkl', 'wb') as f:
        #     #     new_preds = {}
        #     #     for k,v in preds.items():
        #     #         v.pop('pred_masks2d')
        #     #         new_preds[k] = v
        #     #     pickle.dump(new_preds, f)

        def process_single_scene(sample_idx):
            # print('%s sample_idx: %s' % (self.split, sample_idx))
            info = {}
            pc_info = {"num_features": 4, "lidar_idx": sample_idx}
            info["point_cloud"] = pc_info

            image_info = {
                "image_idx": sample_idx,
                "image_shape": self.get_image_shape(sample_idx),
            }
            info["image"] = image_info
            calib = self.get_calib(sample_idx)

            P2 = np.concatenate([calib.P2, np.array([[0.0, 0.0, 0.0, 1.0]])], axis=0)
            R0_4x4 = np.zeros([4, 4], dtype=calib.R0.dtype)
            R0_4x4[3, 3] = 1.0
            R0_4x4[:3, :3] = calib.R0
            V2C_4x4 = np.concatenate(
                [calib.V2C, np.array([[0.0, 0.0, 0.0, 1.0]])], axis=0
            )
            calib_info = {"P2": P2, "R0_rect": R0_4x4, "Tr_velo_to_cam": V2C_4x4}

            info["calib"] = calib_info

            if has_label:
                obj_list = self.get_label(sample_idx)
                annotations = {}
                annotations["name"] = np.array([obj.cls_type for obj in obj_list])
                annotations["truncated"] = np.array(
                    [obj.truncation for obj in obj_list]
                )
                annotations["occluded"] = np.array([obj.occlusion for obj in obj_list])
                annotations["alpha"] = np.array([obj.alpha for obj in obj_list])
                annotations["bbox"] = np.concatenate(
                    [obj.box2d.reshape(1, 4) for obj in obj_list], axis=0
                )
                annotations["dimensions"] = np.array(
                    [[obj.l, obj.h, obj.w] for obj in obj_list]
                )  # lhw(camera) format
                annotations["location"] = np.concatenate(
                    [obj.loc.reshape(1, 3) for obj in obj_list], axis=0
                )
                # annotations['rotation_y'] = np.array([obj.ry for obj in obj_list])
                annotations["rotation_y"] = np.array(
                    [
                        (obj.ry if obj.ry <= -np.pi else (obj.ry + 2 * np.pi))
                        for obj in obj_list
                    ]
                )
                annotations["score"] = np.array([obj.score for obj in obj_list])
                annotations["difficulty"] = np.array(
                    [obj.level for obj in obj_list], np.int32
                )

                num_objects = len(
                    [obj.cls_type for obj in obj_list if obj.cls_type != "DontCare"]
                )
                num_gt = len(annotations["name"])
                index = list(range(num_objects)) + [-1] * (num_gt - num_objects)
                annotations["index"] = np.array(index, dtype=np.int32)

                loc = annotations["location"][:num_objects]
                dims = annotations["dimensions"][:num_objects]
                rots = annotations["rotation_y"][:num_objects]
                loc_lidar = calib.rect_to_lidar(loc)
                l, h, w = dims[:, 0:1], dims[:, 1:2], dims[:, 2:3]
                loc_lidar[:, 2] += h[:, 0] / 2
                gt_boxes_lidar = np.concatenate(
                    [loc_lidar, l, w, h, -(np.pi / 2 + rots[..., np.newaxis])], axis=1
                )
                annotations["gt_boxes_lidar"] = gt_boxes_lidar
                if len(annotations["bbox"]) > 0 and annotations["bbox"][0, 0] == -1:
                    annotations["bbox"] = box_utils.lidar_box_to_image_box(
                        gt_boxes_lidar, calib
                    )[0]

                points = self.get_lidar(sample_idx)
                calib = self.get_calib(sample_idx)
                pts_rect = calib.lidar_to_rect(points[:, 0:3])

                fov_flag = self.get_fov_flag(
                    pts_rect, info["image"]["image_shape"], calib
                )
                pts_fov = points[fov_flag]

                if preds is not None:
                    fake_labels = -torch.ones(len(fov_flag), dtype=torch.long)
                    # fake_labels[fov_flag] = 0
                    pts_img, _ = calib.rect_to_img(pts_rect[fov_flag])
                    pts_img = torch.from_numpy(pts_img)
                    pred = preds[sample_idx]

                    # pred_instances = batch_dict['image_preds']
                    # pts_img = batch_dict['pts_img']
                    # batch_index, pts_img = pts_img[:, 0], pts_img[:, 1:]
                    # pts_target_list = []
                    # pred_masks2d_list = []
                    # for i, (image_shape, pred2d) in enumerate(
                    #         zip(batch_dict['image_shape'], pred_instances)):
                    # new_shape = np.array(pred2d.image_size)
                    pred_boxes2d = torch.from_numpy(pred.get("pred_boxes2d"))
                    pred_labels2d = torch.from_numpy(pred.get("pred_labels2d"))
                    pred_scores2d = torch.from_numpy(pred.get("pred_scores2d"))
                    pred_masks2d = torch.from_numpy(pred.get("pred_masks2d_org"))

                    box_thresh = 0.7
                    if box_thresh > 0:
                        box_mask = pred_scores2d >= box_thresh
                        pred_boxes2d = pred_boxes2d[box_mask]
                        pred_labels2d = pred_labels2d[box_mask]
                        pred_masks2d = pred_masks2d[box_mask]
                        pred_scores2d = pred_scores2d[box_mask]

                    # overlay_label = torch.zeros(*new_shape, dtype=torch.float,
                    #                             device=pred_boxes2d.device)
                    # overlay_score = torch.zeros(*new_shape, dtype=torch.float,
                    #                             device=pred_boxes2d.device)
                    pts_target = torch.zeros(
                        len(pts_img), dtype=torch.long, device=pred_boxes2d.device
                    )

                    wh_matrix = pred_boxes2d[:, 2:] - pred_boxes2d[:, :2]
                    areas = torch.prod(wh_matrix, dim=1)
                    sorted_idxs = torch.argsort(-areas)
                    pred_labels2d = pred_labels2d[sorted_idxs]
                    pred_boxes2d = pred_boxes2d[sorted_idxs]
                    pred_masks2d = pred_masks2d[sorted_idxs]
                    wh_matrix = wh_matrix[sorted_idxs]
                    high_thresh = 0.6
                    low_thresh = 0.3
                    positive_indices = in_2d_box(pts_img, pred_boxes2d)
                    for idx, wh, pm, l2d, box in zip(
                        positive_indices,
                        wh_matrix,
                        pred_masks2d,
                        pred_labels2d.long(),
                        pred_boxes2d,
                    ):
                        if len(idx) == 0:
                            continue
                        pts_box = pts_img[idx]
                        pts_box[:, 0] = 2 * ((pts_box[:, 0] - box[0]) / wh[0]) - 1.0
                        pts_box[:, 1] = 2 * ((pts_box[:, 1] - box[1]) / wh[1]) - 1.0
                        assert pts_box.min() >= -1 and pts_box.max() <= 1.0
                        pts_box = pts_box[None, None, ...]
                        pm = pm[None, ...]
                        pts_score = grid_sample(pm, pts_box)
                        pts_score = torch.squeeze(pts_score)
                        pts_target[idx[pts_score > high_thresh]] = l2d
                        pts_target[
                            idx[
                                torch.logical_and(
                                    pts_score > low_thresh, pts_score < high_thresh
                                )
                            ]
                        ] = -1
                    gt_labels_temp = fake_labels.clone()
                    fake_labels[fov_flag] = pts_target

                    # ground truth
                    points_single = torch.from_numpy(pts_fov[:, :3])
                    point_cls_labels_single = torch.zeros(
                        len(pts_fov), dtype=torch.long, device=pred_boxes2d.device
                    )
                    gt_boxes = torch.from_numpy(gt_boxes_lidar)
                    box_filter = torch.tensor(
                        [
                            i
                            for i, n in enumerate(annotations["name"])
                            if n in self.class_names
                        ],
                        dtype=torch.long,
                    )
                    gt_boxes = gt_boxes[box_filter]

                    gt_labels = torch.tensor(
                        [
                            self.class_names.index(annotations["name"][i]) + 1
                            for i in box_filter
                        ]
                    )
                    # gt_boxes = torch.cat([gt_boxes, gt_labels[:, None]], dim=1)
                    box_idxs_of_pts = (
                        roiaware_pool3d_utils.points_in_boxes_cpu(
                            points_single, gt_boxes
                        )
                        .long()
                        .squeeze(dim=0)
                    )
                    for l, fg_flag in zip(gt_labels, box_idxs_of_pts):
                        point_cls_labels_single[fg_flag] = l

                    # acc
                    gt_labels_temp[fov_flag] = point_cls_labels_single

                    annotations["pts_fake_labels"] = fake_labels.numpy()
                    annotations["pts_gt_labels"] = gt_labels_temp.numpy()
                    # print(acc, recall, precision)
                    # debug
                    # img = self.get_image(sample_idx)
                    # gt_boxes2d = annotations['bbox'][box_filter.numpy()]
                    # debug_utils.save_image_boxes_and_pts_labels_and_mask(
                    #     img,
                    #     gt_boxes2d,
                    #     pts_fov[:,:3],
                    #     pts_target, calib, pred.get('pred_masks2d'),
                    #     img_name=sample_idx+'_fake.png'
                    # )
                    # debug_utils.save_image_boxes_and_pts_labels_and_mask(
                    #     img,
                    #     gt_boxes2d,
                    #     pts_fov[:, :3],
                    #     point_cls_labels_single, calib, pred.get('pred_masks2d'),
                    #     img_name=sample_idx+'_gt.png'
                    # )

                if count_inside_pts:
                    corners_lidar = box_utils.boxes_to_corners_3d(gt_boxes_lidar)
                    num_points_in_gt = -np.ones(num_gt, dtype=np.int32)

                    for k in range(num_objects):
                        flag = box_utils.in_hull(pts_fov[:, 0:3], corners_lidar[k])
                        num_points_in_gt[k] = flag.sum()
                    annotations["num_points_in_gt"] = num_points_in_gt
                info["annos"] = annotations

            return info

        sample_id_list = (
            sample_id_list if sample_id_list is not None else self.sample_id_list
        )
        # with futures.ThreadPoolExecutor(num_workers) as executor:
        #     infos = executor.map(process_single_scene, sample_id_list)
        infos = []
        for idx in tqdm(sample_id_list, total=len(sample_id_list), desc=self.split):
            infos.append(process_single_scene(idx))

        # if 'pts_fake_labels' in infos[0]['annos']:
        #     avg_acc, avg_recall, avg_precision = 0, 0, 0
        #     for info in infos:
        #         pts_fake_label=info['annos']['pts_fake_labels']
        #         pts_gt_label = info['annos']['pts_gt_labels']
        #         valid_mask = pts_fake_label >= 0
        #         y_true = pts_gt_label[valid_mask]
        #         y_pred = pts_fake_label[valid_mask]
        #         acc = accuracy_score(y_true, y_pred)
        #         recall = recall_score(y_true, y_pred)
        #         precision = precision_score(y_true, y_pred)
        #         avg_acc += acc
        #         avg_recall += recall
        #         avg_precision += precision
        #     avg_acc /= len(infos)
        #     avg_recall /= len(infos)
        #     avg_precision /= len(infos)
        #     print('Avg Acc: %.4f, Avg recall: %.4f, Avg precision: %.4f' % (avg_acc, avg_recall, avg_precision))
        return list(infos)

    def create_groundtruth_database(
        self, info_path=None, used_classes=None, split="train"
    ):
        import torch

        database_save_path = Path(self.root_path) / (
            "gt_database" if split == "train" else ("gt_database_%s" % split)
        )
        db_info_save_path = Path(self.root_path) / ("kitti_dbinfos_%s.pkl" % split)

        database_save_path.mkdir(parents=True, exist_ok=True)
        all_db_infos = {}

        with open(info_path, "rb") as f:
            infos = pickle.load(f)

        for k in range(len(infos)):
            print("gt_database sample: %d/%d" % (k + 1, len(infos)))
            info = infos[k]
            sample_idx = info["point_cloud"]["lidar_idx"]
            points = self.get_lidar(sample_idx)
            annos = info["annos"]
            names = annos["name"]
            difficulty = annos["difficulty"]
            bbox = annos["bbox"]
            gt_boxes = annos["gt_boxes_lidar"]

            num_obj = gt_boxes.shape[0]
            point_indices = roiaware_pool3d_utils.points_in_boxes_cpu(
                torch.from_numpy(points[:, 0:3]), torch.from_numpy(gt_boxes)
            ).numpy()  # (nboxes, npoints)

            for i in range(num_obj):
                filename = "%s_%s_%d.bin" % (sample_idx, names[i], i)
                filepath = database_save_path / filename
                gt_points = points[point_indices[i] > 0]

                gt_points[:, :3] -= gt_boxes[i, :3]
                with open(filepath, "w") as f:
                    gt_points.tofile(f)

                if (used_classes is None) or names[i] in used_classes:
                    db_path = str(
                        filepath.relative_to(self.root_path)
                    )  # gt_database/xxxxx.bin
                    db_info = {
                        "name": names[i],
                        "path": db_path,
                        "image_idx": sample_idx,
                        "gt_idx": i,
                        "box3d_lidar": gt_boxes[i],
                        "num_points_in_gt": gt_points.shape[0],
                        "difficulty": difficulty[i],
                        "bbox": bbox[i],
                        "score": annos["score"][i],
                    }
                    if names[i] in all_db_infos:
                        all_db_infos[names[i]].append(db_info)
                    else:
                        all_db_infos[names[i]] = [db_info]
        for k, v in all_db_infos.items():
            print("Database %s: %d" % (k, len(v)))

        with open(db_info_save_path, "wb") as f:
            pickle.dump(all_db_infos, f)

    @staticmethod
    def generate_prediction_dicts(
        batch_dict, pred_dicts, class_names, output_path=None
    ):
        """
        Args:
            batch_dict:
                frame_id:
            pred_dicts: list of pred_dicts
                pred_boxes: (N, 7), Tensor
                pred_scores: (N), Tensor
                pred_labels: (N), Tensor
            class_names:
            output_path:

        Returns:

        """

        def get_template_prediction(num_samples):
            ret_dict = {
                "name": np.zeros(num_samples),
                "truncated": np.zeros(num_samples),
                "occluded": np.zeros(num_samples),
                "alpha": np.zeros(num_samples),
                "bbox": np.zeros([num_samples, 4]),
                "dimensions": np.zeros([num_samples, 3]),
                "location": np.zeros([num_samples, 3]),
                "rotation_y": np.zeros(num_samples),
                "score": np.zeros(num_samples),
                "boxes_lidar": np.zeros([num_samples, 7]),
            }
            return ret_dict

        def generate_single_sample_dict(batch_index, box_dict):
            pred_scores = box_dict["pred_scores"].cpu().numpy()
            pred_boxes = box_dict["pred_boxes"].cpu().numpy()
            pred_labels = box_dict["pred_labels"].cpu().numpy()
            pred_dict = get_template_prediction(pred_scores.shape[0])
            if pred_scores.shape[0] == 0:
                return pred_dict

            calib = batch_dict["calib_org"][batch_index]
            image_shape = batch_dict["image_shape"][batch_index]
            pred_boxes_camera = box_utils.boxes3d_lidar_to_kitti_camera(
                pred_boxes, calib
            )
            pred_boxes_img = box_utils.boxes3d_kitti_camera_to_imageboxes(
                pred_boxes_camera, calib, image_shape=image_shape
            )

            pred_dict["name"] = np.array(class_names)[pred_labels - 1]
            pred_dict["alpha"] = (
                -np.arctan2(-pred_boxes[:, 1], pred_boxes[:, 0])
                + pred_boxes_camera[:, 6]
            )
            pred_dict["bbox"] = pred_boxes_img
            pred_dict["dimensions"] = pred_boxes_camera[:, 3:6]
            pred_dict["location"] = pred_boxes_camera[:, 0:3]
            pred_dict["rotation_y"] = pred_boxes_camera[:, 6]
            pred_dict["score"] = pred_scores
            pred_dict["boxes_lidar"] = pred_boxes

            return pred_dict

        annos = []
        for index, box_dict in enumerate(pred_dicts):
            frame_id = batch_dict["frame_id"][index]

            single_pred_dict = generate_single_sample_dict(index, box_dict)
            single_pred_dict["frame_id"] = frame_id
            annos.append(single_pred_dict)

            if output_path is not None:
                cur_det_file = output_path / ("%s.txt" % frame_id)
                with open(cur_det_file, "w") as f:
                    bbox = single_pred_dict["bbox"]
                    loc = single_pred_dict["location"]
                    dims = single_pred_dict["dimensions"]  # lhw -> hwl

                    for idx in range(len(bbox)):
                        print(
                            "%s -1 -1 %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f"
                            % (
                                single_pred_dict["name"][idx],
                                single_pred_dict["alpha"][idx],
                                bbox[idx][0],
                                bbox[idx][1],
                                bbox[idx][2],
                                bbox[idx][3],
                                dims[idx][1],
                                dims[idx][2],
                                dims[idx][0],
                                loc[idx][0],
                                loc[idx][1],
                                loc[idx][2],
                                single_pred_dict["rotation_y"][idx],
                                single_pred_dict["score"][idx],
                            ),
                            file=f,
                        )

        return annos

    def evaluation(self, det_annos, class_names, **kwargs):
        if "annos" not in self.kitti_infos[0].keys():
            return None, {}

        from .kitti_object_eval_python import eval as kitti_eval

        eval_det_annos = copy.deepcopy(det_annos)
        eval_gt_annos = [copy.deepcopy(info["annos"]) for info in self.kitti_infos]
        ap_result_str, ap_dict = kitti_eval.get_official_eval_result(
            eval_gt_annos, eval_det_annos, class_names
        )

        return ap_result_str, ap_dict

    def __len__(self):
        if self._merge_all_iters_to_one_epoch:
            return len(self.kitti_infos) * self.total_epochs

        return len(self.kitti_infos)

    def __getitem__(self, index):
        if self._merge_all_iters_to_one_epoch:
            index = index % len(self.kitti_infos)
        info = copy.deepcopy(self.kitti_infos[index])

        sample_idx = info["point_cloud"]["lidar_idx"]

        points = self.get_lidar(sample_idx)
        calib = self.get_calib(sample_idx)
        image = self.get_image(sample_idx)

        img_shape = info["image"]["image_shape"]

        if self.dataset_cfg.FOV_POINTS_ONLY:
            pts_rect = calib.lidar_to_rect(points[:, 0:3])
            fov_flag = get_fov_flag(pts_rect, img_shape, calib)
            fov_indices = np.nonzero(fov_flag)[0]
            points = points[fov_flag]

        input_dict = {
            "points": points,
            "images": image,
            "frame_id": sample_idx,
            "calib": calib,
            "calib_org": copy.deepcopy(calib),
            "image_shape": img_shape,
            "fov_indices": fov_indices,
        }
        if self.fake_boxes is not None:
            box_dict = self.fake_boxes[sample_idx]
            box3d = box_dict["pred_boxes"]
            labels = box_dict["pred_labels"].astype(np.float32)
            harmonic_mean = (
                2
                * box_dict["pred_scores"]
                * box_dict["pred_scores2d"]
                / (box_dict["pred_scores"] + box_dict["pred_scores2d"] + 1e-8)
            )
            ignore_mask = harmonic_mean < self.box_thresh
            labels[ignore_mask] = -1
            input_dict.update(
                {
                    "gt_classes": labels,
                    "gt_boxes": box3d,
                    "gt_boxes_2d": box_dict["pred_boxes2d"],
                }
            )
        elif "annos" in info:
            annos = info["annos"]
            annos = common_utils.drop_info_with_name(annos, name="DontCare")
            loc, dims, rots = (
                annos["location"],
                annos["dimensions"],
                annos["rotation_y"],
            )
            gt_names = annos["name"]
            # gt_names[gt_names == 'Van'] = 'Car' # TODO remove this row
            gt_boxes_camera = np.concatenate(
                [loc, dims, rots[..., np.newaxis]], axis=1
            ).astype(np.float32)
            gt_boxes_lidar = box_utils.boxes3d_kitti_camera_to_lidar(
                gt_boxes_camera, calib
            )
            # if len(annos['bbox']) > 0 and annos['bbox'][0, 0] == -1:
            #     annos['bbox'] = box_utils.lidar_box_to_image_box(gt_boxes_lidar, calib)[0]
            input_dict.update(
                {
                    "gt_names": gt_names,
                    "gt_boxes": gt_boxes_lidar,
                    "gt_boxes_2d": annos["bbox"],
                }
            )
            road_plane = self.get_road_plane(sample_idx)
            if road_plane is not None:
                input_dict["road_plane"] = road_plane
        else:
            input_dict.update(
                {
                    "gt_names": np.empty(0),
                    "gt_boxes": np.empty((0, 7)),
                    "gt_boxes_2d": np.empty((0, 4)),
                }
            )

        # debug_utils.save_image_boxes_and_pts(
        #     input_dict['images'],
        #     input_dict['gt_boxes_2d'],
        #     input_dict['points'][:, :3],
        #     input_dict['calib'],
        #     img_name=f'{input_dict["frame_id"]}_org.png',
        # )

        data_dict = self.prepare_data(data_dict=input_dict)
        if sample_idx == data_dict["frame_id"] and "gt_boxes" in data_dict:
            data_dict["box_labels"] = data_dict["gt_boxes"][:, -1]
        if "gt_classes" in data_dict:
            data_dict.pop("gt_classes")

        # if self.fake_label and sample_idx == data_dict['frame_id']:
        #     box3d = data_dict.pop('gt_boxes')
        #     fov_indices = data_dict['fov_indices']
        #     data_dict['pts_fake_labels'] = info['annos']['pts_fake_labels'][fov_indices]

        # if data_dict['frame_id'] != input_dict['frame_id']:
        #     data_dict2 = self.prepare_data(data_dict=input_dict)
        #     data_dict2 = self.prepare_data(data_dict=input_dict)
        #     data_dict2 = self.prepare_data(data_dict=input_dict)

        # mean = np.asarray(self.dataset_cfg.DATA_PROCESSOR[1].MEAN)[:, None, None]
        # std = np.asarray(self.dataset_cfg.DATA_PROCESSOR[1].STD)[:, None, None]
        # debug_image = data_dict['images'] * std + mean
        # debug_image = np.ascontiguousarray(np.transpose(debug_image, (1, 2, 0)))
        # debug_image = np.ascontiguousarray(debug_image[:, :, ::-1])
        #
        # debug_utils.save_image_boxes_and_pts(
        #     debug_image,
        #     data_dict['gt_boxes_2d'],
        #     data_dict['points'][:, :3],
        #     data_dict['calib'],
        #     img_name=f'{input_dict["frame_id"]}_fake.png',
        # )

        # debug_utils.save_image_boxes_and_pts(
        #     debug_image,
        #     data_dict['gt_boxes_2d'],
        #     data_dict['points'][:, :3],
        #     data_dict['calib'],
        #     img_name=f'{input_dict["frame_id"]}_org.png',
        # )
        #
        # debug_utils.save_image_boxes_and_pts_labels_and_mask(
        #     debug_image,
        #     data_dict['gt_boxes_2d'],
        #     data_dict['points'][:, :3],
        #     data_dict['pts_fake_labels'], calib, [],
        #     img_name=sample_idx+'_fake.png'
        # )
        #
        # debug_utils.save_image_boxes_and_pts(
        #     debug_image,
        #     data_dict['gt_boxes_2d'],
        #     data_dict['points'][:, :3],
        #     data_dict['calib'],
        #     img_name=f'{data_dict["frame_id"]}_aug.png',
        # )

        # pts_rect = data_dict['calib'].lidar_to_rect(data_dict['points'][:, 0:3])
        # _, pts_img = get_fov_flag(pts_rect, img_shape, data_dict['calib'], True)
        # try:
        #     assert np.allclose(pts_img, data_dict['pts_img'])
        # except AssertionError:
        #     print()
        # assert len(data_dict['gt_boxes_2d']) == len(data_dict['gt_boxes'])
        # assert len(data_dict['pts_img']) == len(data_dict['points'])

        return data_dict

    def prepare_data(self, data_dict):
        """
        Args:
            data_dict:
                points: (N, 3 + C_in)
                gt_boxes: optional, (N, 7 + C) [x, y, z, dx, dy, dz, heading, ...]
                gt_names: optional, (N), string
                ...

        Returns:
            data_dict:
                frame_id: string
                points: (N, 3 + C_in)
                gt_boxes: optional, (N, 7 + C) [x, y, z, dx, dy, dz, heading, ...]
                gt_names: optional, (N), string
                use_lead_xyz: bool
                voxels: optional (num_voxels, max_points_per_voxel, 3 + C)
                voxel_coords: optional (num_voxels, 3)
                voxel_num_points: optional (num_voxels)
                ...
        """
        if self.training:
            assert "gt_boxes" in data_dict, "gt_boxes should be provided for training"
            if "gt_classes" in data_dict:
                gt_boxes_mask = np.ones(len(data_dict["gt_classes"]), dtype=bool)
            else:
                gt_boxes_mask = np.array(
                    [n in self.class_names for n in data_dict["gt_names"]],
                    dtype=np.bool_,
                )

            data_dict = self.data_augmentor.forward(
                data_dict={**data_dict, "gt_boxes_mask": gt_boxes_mask}
            )
        skip = False
        if "gt_classes" in data_dict:
            gt_boxes = np.concatenate(
                (
                    data_dict["gt_boxes"],
                    data_dict["gt_classes"].reshape(-1, 1).astype(np.float32),
                ),
                axis=1,
            )
            skip = np.all(data_dict["gt_classes"] == -1)
            data_dict["gt_boxes"] = gt_boxes
        elif data_dict.get("gt_boxes", None) is not None:
            selected = common_utils.keep_arrays_by_name(
                data_dict["gt_names"], self.class_names
            )
            data_dict["gt_boxes"] = data_dict["gt_boxes"][selected]
            data_dict["gt_boxes_2d"] = data_dict["gt_boxes_2d"][selected]
            data_dict["gt_names"] = data_dict["gt_names"][selected]
            gt_classes = np.array(
                [self.class_names.index(n) + 1 for n in data_dict["gt_names"]],
                dtype=np.int32,
            )
            gt_boxes = np.concatenate(
                (data_dict["gt_boxes"], gt_classes.reshape(-1, 1).astype(np.float32)),
                axis=1,
            )
            data_dict["gt_boxes"] = gt_boxes

        data_dict = self.point_feature_encoder.forward(data_dict)

        data_dict = self.data_processor.forward(data_dict=data_dict)
        if self.training and "gt_classes" in data_dict:
            return data_dict

        if self.training and len(data_dict["gt_boxes"]) == 0:
            new_index = np.random.randint(self.__len__())
            return self.__getitem__(new_index)

        data_dict.pop("gt_names", None)
        return data_dict

    @staticmethod
    def collate_batch(batch_list, _unused=False):
        data_dict = defaultdict(list)

        skip_keys = ["gt_boxes_2d"]
        gt_instances = []

        for cur_sample in batch_list:
            instance = Instances(tuple(cur_sample["image_shape"]))
            boxes = Boxes(cur_sample["gt_boxes_2d"])
            classes = torch.from_numpy(cur_sample["box_labels"]).long()
            instance.gt_boxes = boxes
            instance.gt_classes = classes
            gt_instances.append(instance)

            for key, val in cur_sample.items():
                if key in skip_keys:
                    continue
                else:
                    data_dict[key].append(val)
        batch_size = len(batch_list)
        ret = {"instances": gt_instances}

        for key, val in data_dict.items():
            try:
                if key in ["voxels", "voxel_num_points"]:
                    ret[key] = np.concatenate(val, axis=0)
                elif key in ["pts_img", "points", "voxel_coords"]:
                    coors = []
                    for i, coor in enumerate(val):
                        coor_pad = np.pad(
                            coor, ((0, 0), (1, 0)), mode="constant", constant_values=i
                        )
                        coors.append(coor_pad)
                    ret[key] = np.concatenate(coors, axis=0)
                elif key == "pts_fake_labels":
                    ret[key] = np.concatenate(val, axis=0)
                elif key in ["gt_boxes", "gt_boxes_2d"]:
                    max_gt = max([len(x) for x in val])
                    batch_gt_boxes3d = np.zeros(
                        (batch_size, max_gt, val[0].shape[-1]), dtype=np.float32
                    )
                    for k in range(batch_size):
                        batch_gt_boxes3d[k, : val[k].__len__(), :] = val[k]
                    ret[key] = batch_gt_boxes3d
                elif key == "images":
                    ret[key] = np.stack(val, axis=0)
                elif key == "calib":
                    ret[key] = val
                elif key == "box_labels":
                    continue
                    # ret['torch_calib'] = [TorchCalibration(calib) for calib in val]
                else:
                    ret[key] = np.stack(val, axis=0)
            except:
                print("Error in collate_batch: key=%s" % key)
                raise TypeError

        ret["batch_size"] = batch_size
        return ret


def create_kitti_infos(dataset_cfg, class_names, data_path, save_path, workers=4):
    dataset = KittiFusionDataset(
        dataset_cfg=dataset_cfg,
        class_names=class_names,
        root_path=data_path,
        training=False,
    )
    train_split, val_split = "train", "val"

    train_filename = save_path / ("kitti_infos_%s.pkl" % train_split)
    val_filename = save_path / ("kitti_infos_%s.pkl" % val_split)
    trainval_filename = save_path / "kitti_infos_trainval.pkl"
    # test_filename = save_path / 'kitti_infos_test.pkl'

    print("---------------Start to generate data infos---------------")

    dataset.set_split(val_split)
    kitti_infos_val = dataset.get_infos(
        num_workers=workers, has_label=True, count_inside_pts=True
    )
    with open(val_filename, "wb") as f:
        pickle.dump(kitti_infos_val, f)
    print("Kitti info val file is saved to %s" % val_filename)

    dataset.set_split(train_split)
    kitti_infos_train = dataset.get_infos(
        num_workers=workers,
        has_label=dataset_cfg.get("HAS_LABEL", True),
        count_inside_pts=True,
    )
    with open(train_filename, "wb") as f:
        pickle.dump(kitti_infos_train, f)
    print("Kitti info train file is saved to %s" % train_filename)

    with open(trainval_filename, "wb") as f:
        pickle.dump(kitti_infos_train + kitti_infos_val, f)
    print("Kitti info trainval file is saved to %s" % trainval_filename)

    # dataset.set_split('test')
    # kitti_infos_test = dataset.get_infos(num_workers=workers, has_label=False, count_inside_pts=False)
    # with open(test_filename, 'wb') as f:
    #     pickle.dump(kitti_infos_test, f)
    # print('Kitti info test file is saved to %s' % test_filename)
    if dataset_cfg.get("HAS_LABEL", True):
        print(
            "---------------Start create groundtruth database for data augmentation---------------"
        )
        dataset.set_split(train_split)
        dataset.create_groundtruth_database(train_filename, split=train_split)

    print("---------------Data preparation Done---------------")
