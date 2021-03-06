import os, sys
sys.path.append(os.getcwd())
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from vfa.data.wildtrack import Wildtrack
from vfa.data.multiviewX import MultiviewX
from vfa.data.multiviewC import MultiviewC
from vfa.utils import project

EPSILON = 1e-6
MAXIMUM_AREA_RATIO = 0.3

""" 
#--------------------------------------#
-    Convert worldgrid to worldcoord   -
#--------------------------------------#
"""
# MultiviewC
def mc_convert(grid, scale=1.):
    return grid / scale

# MultiviewX
def mx_convert(grid, scale=40.):
    return grid / scale

# Wildtrack
def wt_convert(grid, scale=2.5):
    grid[..., 0] = grid[..., 0] * scale - 300   # x
    grid[..., 1] = grid[..., 1] * scale - 900   # y
    grid[..., 2] = grid[..., 2] * scale         # z
    return grid

def convert(grid, args):
    if args.data == MultiviewC.__name__:
        coord = mc_convert(grid)
    elif args.data == MultiviewX.__name__:
        coord = mx_convert(grid)
    elif args.data == Wildtrack.__name__:
        coord = wt_convert(grid)
    return coord

class VFA(nn.Module):
    def __init__(self, channel, grid_height=160, cube_size=(25, 25, 32), feat_scale=1, args=None):
        super(VFA, self).__init__()
        self.cube_height = cube_size[2]
        z_corners = torch.arange(0, grid_height, cube_size[2])
        z_corners = F.pad(z_corners.view(-1, 1, 1, 1), [2, 0])
        num_grid_layer = len(z_corners)
        corners_offset = torch.Tensor(self.generate_cube(cube_size)).view(1, 1, 1, 1, 8, 3)
        self.register_buffer('z_corners', z_corners)
        self.register_buffer('corners_offset', corners_offset)

        self.feat_scale = feat_scale
        self.args = args
        self.collapse = nn.Linear(channel * num_grid_layer, channel)

    def forward(self, feature, calib, grid, crange=(-1, 0.95), visualize=False):
        # feature: (1, 512, 90, 160), calib: (3, 4), grid: (1, 156, 156, 3) z_corners: (8, 1, 1, 3)
        # corners: (1, 5, 156, 156, 3) = grid: (1, 1, 156, 156, 3) + z_corners: (5, 1, 1, 3)
        corners = grid.unsqueeze(0) + self.z_corners.view(-1, 1, 1, 3)
        corners = corners.unsqueeze(-2) #(1, 5, 156, 156, 1, 3)
        corners3d = corners.repeat((1,1,1,1,8,1)) + self.corners_offset.to(device=corners.device) #(1, 5, 156, 156, 8, 3)
        # convert worldgrid to world coord
        corners3d = convert(corners3d, self.args)

        calib = calib.view(-1, 1, 1, 1, 1, 3, 4)
        img_corners3d = project(corners3d, calib) #(1, 5, 156, 156, 8, 2)
        
        feature_height, feature_width = feature.size()[2:]
        # img_size = corners.new([feature_width, feature_height]) / self.feat_scale
        img_size = corners.new(self.args.image_size[::-1])
        norm_corners3d = (2 * img_corners3d / img_size - 1).clamp(crange[0], crange[1]) #(1, 5, 156, 156, 8, 2)
        # norm_corners3d = (2 * img_corners3d / img_size - 1).clamp(-1, 1) #(1, 5, 156, 156, 8, 2) # Adjust the influence of the image boundary

        # norm_corners size: (B, nl, L, W, 4) batch_size, number of layer, length of grid, width of grid, format
        # format: Left, Top, Right, Bottom
        box_corners = torch.cat([
            torch.min(norm_corners3d[..., 0], dim=-1, keepdim=True)[0],
            torch.min(norm_corners3d[..., 1], dim=-1, keepdim=True)[0],
            torch.max(norm_corners3d[..., 0], dim=-1, keepdim=True)[0],
            torch.max(norm_corners3d[..., 1], dim=-1, keepdim=True)[0],
        ], dim=-1)
        batch, _, length, width, _ = box_corners.shape
        box_corners = box_corners.flatten(2, 3) 

        if visualize:
            centers3d = corners.clone()
            centers3d[..., -1] += self.cube_height * 0.5
            # convert worldgrid to world coord
            centers3d = convert(centers3d, self.args)
            
            img_corners_center = project(centers3d, calib)
            norm_corners_center = img_corners_center / img_size #(1, 5, 156, 156, 8, 2)
            box_center = norm_corners_center.flatten(2, 3) 
            # transform the box_corners range from [-1, 1] to [0, 1]
            viz_box_corners = ( box_corners + 1 ) / 2
            self.visualize_cube(feature, viz_box_corners, box_center)
        
        # Compute the area of each bounding box
        area = (((box_corners[..., 2:] - box_corners[..., :2]).prod(dim=-1)) \
                 * feature_height * feature_width + EPSILON).unsqueeze(1)
        visible = torch.logical_and(area > EPSILON, area < (feature_height*feature_width*MAXIMUM_AREA_RATIO))
        # visible = (area > EPSILON) # REMOVE the areas that are too small or too big

        # Sample the integral image at bounding box locations
        intergral_img = self.integral_image(feature)
        # box_corners size: (B, C, L*W, 4) format: Left, Top, Right, Bottom
        left_top = F.grid_sample(intergral_img, box_corners[..., [0, 1]])
        right_btm = F.grid_sample(intergral_img, box_corners[..., [2, 3]])
        right_top = F.grid_sample(intergral_img, box_corners[..., [2, 1]])
        left_btm = F.grid_sample(intergral_img, box_corners[..., [0, 3]])

        # Compute the voxel feature
        vox_features = (left_top + right_btm - right_top - left_btm) / area # (B, C, nl, L*W)
        vox_features = vox_features * visible
        vox_features = vox_features.permute(0, 3, 1, 2).flatten(0,1).flatten(1,2) # (B*L*W, C*nl)

        # Collapse to orthographic feature map 
        ortho_features = self.collapse(vox_features).view(batch, length, width, -1) # (B, L, W, C)
        ortho_features = F.relu(ortho_features.permute(0, 3, 1, 2), inplace=True)
        return ortho_features

    def generate_cube(self, cub_size):
        l, w, h = cub_size
        x = [-l / 2, l / 2, l / 2, -l / 2, -l / 2, l / 2, l / 2, -l / 2]
        y = [-w / 2, -w / 2, w / 2, w / 2, -w / 2, -w / 2, w / 2, w / 2]
        z = [0, 0, 0, 0, h, h, h, h]
        corners_3d = np.vstack([x, y, z]).T # (8, 3)
        return corners_3d
    
    def visualize_cube(self, feature, box_corners, box_centers, viz_interval=10, viz_center=False, viz_rect=True):
        viz_feature = torch.norm(feature, dim=1).squeeze(0).squeeze(0).detach().cpu().numpy().astype(np.uint8)
        f_H, f_W = viz_feature.shape
        fig = plt.figure(figsize=(15, 8))
        ax = fig.add_subplot(111)
        # ONLY visualize the first layer of grid
        box_corners = box_corners[0, 0]
        box_centers = box_centers[0, 0].squeeze(-2)
        
        # visualize the box and centers at intervals
        mask = torch.arange(0, box_corners.shape[0], step=viz_interval)
        box_corners = box_corners[mask]
        box_centers = box_centers[mask]

        box_corners *= torch.tensor([f_W, f_H, f_W, f_H])[None, :]
        box_centers *= torch.tensor([f_W, f_H])[None, :]
        width = box_corners[:, 2] - box_corners[:, 0]
        height = box_corners[:, 3] - box_corners[:, 1]
        mask = torch.logical_and(((width * height)>0), ((width * height)<=0.3*f_W*f_H))
        box_corners = box_corners[mask]
        box_centers = box_centers[mask]
        width  = width[mask]
        height = height[mask]
        if viz_rect:
            for i in range(len(box_corners)):
                rect = patches.Rectangle((box_corners[i, 0], box_corners[i, 1]), width[i], height[i], edgecolor='red', linewidth=1, fill=False)
                ax.add_patch(rect)
        if viz_center:
            ax.scatter(box_centers[:, 0], box_centers[:, 1], s=2, c='black')
        ax.imshow(viz_feature)
        ax.axis('off')
        plt.xlim(0, f_W)
        plt.ylim(f_H, 0)
        plt.show()
        
        

    def integral_image(self, features):
        return torch.cumsum(torch.cumsum(features, dim=-1), dim=-2)

     
            
