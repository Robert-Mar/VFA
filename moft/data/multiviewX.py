import os, json, re, sys
sys.path.append(os.getcwd())
import numpy as np
import cv2
from PIL import Image
from torchvision.datasets import VisionDataset
from scipy.sparse import coo_matrix
from moft.utils import Obj2D 
from moft.data.GK import GaussianKernel

intrinsic_camera_matrix_filenames = ['intr_Camera1.xml', 'intr_Camera2.xml', 'intr_Camera3.xml', 'intr_Camera4.xml',
                                     'intr_Camera5.xml', 'intr_Camera6.xml']
extrinsic_camera_matrix_filenames = ['extr_Camera1.xml', 'extr_Camera2.xml', 'extr_Camera3.xml', 'extr_Camera4.xml',
                                     'extr_Camera5.xml', 'extr_Camera6.xml']

MULTIVIEWC_BBOX_LABEL_NAMES = ['Person']

class MultiviewX(VisionDataset):
    grid_reduce = 4
    img_reduce = 4
    def __init__(self, root, # The Path of MultiviewX
                       world_size = [640, 1000],
                       img_size = [1080, 1920],
                       cube_LWH = [4, 4, 36], # need to scaled!
                       force_download=False, 
                       reload_GK=False):
        super().__init__(root)
        # MultiviewX has xy-indexing: H*W=640*1000, thus x is \in [0,1000), y \in [0,640)
        # MultiviewX has consistent unit: meter (m) for calibration & pos annotation
        self.__name__ = 'MultiviewX'
        self.num_cam, self.num_frame = 6, 400
        self.img_shape, self.world_size, self.cube_LWH = img_size, world_size, cube_LWH # H,W; N_row,N_col; h w l of cube
        self.grid_reduce, self.img_reduce = MultiviewX.grid_reduce, MultiviewX.img_reduce 
        self.reduced_grid_size = list(map(lambda x: int(x / self.grid_reduce), self.world_size)) # 160, 250
        self.reload_GK = reload_GK
        self.label_names = MULTIVIEWC_BBOX_LABEL_NAMES
        self.intrinsic_matrices, self.extrinsic_matrices = zip(
            *[self.get_intrinsic_extrinsic_matrix(cam) for cam in range(self.num_cam)])

        self.GK = GaussianKernel()
        # different from 3D detection task, we only focus on the location detection on MultiviewX 
        # # different from 3D detection task. Thus, `classAverage` is None by default.
        self.classAverage = None 
        self.labels, self.heatmaps = self.download()

        # Create gt.txt file to evaluate MODA, MODP, prec, rcll metrics
        self.gt_fpath = os.path.join(self.root, 'gt.txt')
        if not os.path.exists(self.gt_fpath) or force_download:
            self.prepare_gt()

    def get_image_fpaths(self, frame_range):
        img_fpaths = {cam: {} for cam in range(1, self.num_cam+1)}
        for camera_folder in sorted(os.listdir(os.path.join(self.root, 'Image_subsets'))):
            cam = int(camera_folder[-1]) 
            if cam >= self.num_cam+1:
                continue
            for fname in sorted(os.listdir(os.path.join(self.root, 'Image_subsets', camera_folder))):
                frame = int(fname.split('.')[0])
                if frame in frame_range:
                    img_fpaths[cam][frame] = os.path.join(self.root, 'Image_subsets', camera_folder, fname)
        return img_fpaths
    
    @staticmethod
    def get_worldgrid_from_pos(pos):
        grid_x = pos % 1000
        grid_y = pos // 1000
        return np.array([grid_x, grid_y], dtype=int)
    
    @staticmethod
    def get_pos_from_worldgrid(worldgrid):
        grid_x, grid_y = worldgrid
        return grid_x + grid_y * 1000

    @staticmethod
    def get_worldgrid_from_worldcoord(world_coord):
        # datasets default unit: centimeter & origin: (-300,-900)
        coord_x, coord_y = world_coord
        grid_x = coord_x * 40
        grid_y = coord_y * 40
        return np.array([grid_x, grid_y], dtype=int)
    
    @staticmethod
    def get_worldcoord_from_worldgrid(worldgrid):
        # datasets default unit: centimeter & origin: (-300,-900)
        grid_x, grid_y = worldgrid
        coord_x = grid_x / 40
        coord_y = grid_y / 40
        return np.array([coord_x, coord_y])


    def get_worldcoord_from_pos(self, pos):
        grid = self.get_worldgrid_from_pos(pos)
        return self.get_worldcoord_from_worldgrid(grid)

    def get_pos_from_worldcoord(self, world_coord):
        grid = self.get_worldgrid_from_worldcoord(world_coord)
        return self.get_pos_from_worldgrid(grid)

    def get_intrinsic_extrinsic_matrix(self, camera_i):
        intrinsic_camera_path = os.path.join(self.root, 'calibrations', 'intrinsic')
        fp_calibration = cv2.FileStorage(os.path.join(intrinsic_camera_path,
                                                      intrinsic_camera_matrix_filenames[camera_i]),
                                         flags=cv2.FILE_STORAGE_READ)
        intrinsic_matrix = fp_calibration.getNode('camera_matrix').mat()
        fp_calibration.release()

        extrinsic_camera_path = os.path.join(self.root, 'calibrations', 'extrinsic')
        fp_calibration = cv2.FileStorage(os.path.join(extrinsic_camera_path,
                                                      extrinsic_camera_matrix_filenames[camera_i]),
                                         flags=cv2.FILE_STORAGE_READ)
        rvec, tvec = fp_calibration.getNode('rvec').mat().squeeze(), fp_calibration.getNode('tvec').mat().squeeze()
        fp_calibration.release()

        rotation_matrix, _ = cv2.Rodrigues(rvec)
        translation_matrix = np.array(tvec, dtype=np.float32).reshape(3, 1)
        extrinsic_matrix = np.hstack((rotation_matrix, translation_matrix))

        return intrinsic_matrix, extrinsic_matrix

    def read_pom(self):
        bbox_by_pos_cam = {}
        cam_pos_pattern = re.compile(r'(\d+) (\d+)')
        cam_pos_bbox_pattern = re.compile(r'(\d+) (\d+) ([-\d]+) ([-\d]+) (\d+) (\d+)')
        with open(os.path.join(self.root, 'rectangles.pom'), 'r') as fp:
            for line in fp:
                if 'RECTANGLE' in line:
                    cam, pos = map(int, cam_pos_pattern.search(line).groups())
                    if pos not in bbox_by_pos_cam:
                        bbox_by_pos_cam[pos] = {}
                    if 'notvisible' in line:
                        bbox_by_pos_cam[pos][cam] = None
                    else:
                        cam, pos, left, top, right, bottom = map(int, cam_pos_bbox_pattern.search(line).groups())
                        bbox_by_pos_cam[pos][cam] = [max(left, 0), max(top, 0),
                                                     min(right, 1920 - 1), min(bottom, 1080 - 1)]
        return bbox_by_pos_cam

    def prepare_gt(self):
        og_gt = []
        for fname in sorted(os.listdir(os.path.join(self.root, 'annotations_positions'))):
            frame = int(fname.split('.')[0])
            with open(os.path.join(self.root, 'annotations_positions', fname)) as json_file:
                all_pedestrians = json.load(json_file)
            for single_pedestrian in all_pedestrians:
                def is_in_cam(cam):
                    return not (single_pedestrian['views'][cam]['xmin'] == -1 and
                                single_pedestrian['views'][cam]['xmax'] == -1 and
                                single_pedestrian['views'][cam]['ymin'] == -1 and
                                single_pedestrian['views'][cam]['ymax'] == -1)

                in_cam_range = sum(is_in_cam(cam) for cam in range(self.num_cam))
                if not in_cam_range:
                    continue
                grid_x, grid_y = self.base.get_worldgrid_from_pos(single_pedestrian['positionID'])
                og_gt.append(np.array([frame, grid_x, grid_y]))
        og_gt = np.stack(og_gt, axis=0)
        os.makedirs(os.path.dirname(self.gt_fpath), exist_ok=True)
        np.savetxt(self.gt_fpath, og_gt, '%d')

    # TODO: check: xy ? yx? Done, yx
    def download(self):

        labels = list()
        # if GK not exist (true), build GK; else, load GK from file. (GK: gaussian kernel heatmap)
        BuildGK = self.reload_GK or not self.GK.GKExist() 
        for fname in sorted(os.listdir(os.path.join(self.root, 'annotations_positions'))):
            # frame = int(fname.split('.')[0])
            with open(os.path.join(self.root, 'annotations_positions', fname)) as json_file:
                all_pedestrians = json.load(json_file)
            i_s, j_s, v_s = [], [], []
            man_infos = list()
            
            for single_pedestrian in all_pedestrians:
                x, y = self.get_worldgrid_from_pos(single_pedestrian['positionID'])
                location = np.array([x, y, np.zeros_like(x, dtype=x.dtype)])
                man_infos.append(Obj2D(classname='Person', location=location, conf=None))

                if BuildGK:
                    i_s.append(int(y / self.grid_reduce))
                    j_s.append(int(x / self.grid_reduce))
                    v_s.append(1)
            if BuildGK:
                occupancy_map = coo_matrix((v_s, (i_s, j_s)), shape=self.reduced_grid_size)
                self.GK.add_item(occupancy_map.toarray())

            labels.append(man_infos)
            
        if BuildGK:
            # dump RGK to file
            heatmaps = self.GK.dump_to_file()
        else:
            heatmaps = self.GK.load_from_file()
        
        return labels, heatmaps
                
    
    