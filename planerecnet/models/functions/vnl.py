import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


class VNL_Loss(nn.Module):
    def __init__(self, input_size,
                 delta_cos=0.867,
                 delta_z=0.0001, sample_ratio=0.3):
        super(VNL_Loss, self).__init__()
        self.input_size = input_size
        self.u0 = torch.tensor(input_size[1] // 2, dtype=torch.float32).cuda()  # x, y focal center
        self.v0 = torch.tensor(input_size[0] // 2, dtype=torch.float32).cuda()
        self.init_image_coor()
        self.delta_cos = delta_cos
        self.delta_z = delta_z
        self.sample_ratio = sample_ratio

    def init_image_coor(self):  # take care of point cloud
        x_row = np.arange(0, self.input_size[1])
        x = np.tile(x_row, (self.input_size[0], 1))
        x = x[np.newaxis, :, :]
        x = x.astype(np.float32)
        x = torch.from_numpy(x.copy()).cuda()
        self.u_u0 = x - self.u0

        y_col = np.arange(0, self.input_size[0])  # y_col = np.arange(0, height)
        y = np.tile(y_col, (self.input_size[1], 1)).T
        y = y[np.newaxis, :, :]
        y = y.astype(np.float32)
        y = torch.from_numpy(y.copy()).cuda()
        self.v_v0 = y - self.v0

    def transfer_xyz(self, depth, k_maritix):  # take care of point cloud
        fx = k_maritix[0, 0]
        fy = k_maritix[1, 1]
        x = self.u_u0 * torch.abs(depth) / fx
        y = self.v_v0 * torch.abs(depth) / fy
        z = depth
        pw = torch.cat([x, y, z], 0).permute(1, 2, 0)
        return pw

    def select_index(self, num):
        valid_width = self.input_size[1]
        valid_height = self.input_size[0]
        if not num <= valid_width * valid_height:
            raise AssertionError()
        p1 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p1)
        p2 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p2)
        p3 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p3)
        p123 = {'p1_x': p1, 'p2_x': p2, 'p3_x': p3}
        return p123

    def form_pw_groups(self, p123, pw):
        """
        Form 3D points groups, with 3 points in each grouup.
        :param p123: points index
        :param pw: 3D points
        :return:
        """
        p1_x = p123['p1_x']
        p2_x = p123['p2_x']
        p3_x = p123['p3_x']
        pw1 = pw[p1_x, :]
        pw2 = pw[p2_x, :]
        pw3 = pw[p3_x, :]
        # [B, N, 3(x,y,z), 3(p1,p2,p3)]
        pw_groups = torch.cat([pw1[:, :, np.newaxis], pw2[:, :, np.newaxis], pw3[:, :, np.newaxis]], 2)
        return pw_groups

    def filter_mask(self, p123, point_cloud, delta_cos=0.985,
                    delta_diff=0.005):
        pw = self.form_pw_groups(p123, point_cloud)
        pw12 = pw[:, :, 1] - pw[:, :, 0]
        pw13 = pw[:, :, 2] - pw[:, :, 0]
        pw23 = pw[:, :, 2] - pw[:, :, 1]

        ###ignore linear
        pw_diff = torch.cat([pw12[:, :, np.newaxis], pw13[:, :, np.newaxis], pw23[:, :, np.newaxis]], 2)  # [n, 3, 3]
        groups, coords, index = pw_diff.shape
        proj_query = pw_diff.permute(0, 2, 1)  # [bn, 3(p123), 3(xyz)]
        proj_key = pw_diff  # [bn, 3(xyz), 3(p123)]
        q_norm = proj_query.norm(2, dim=2)
        nm = torch.bmm(q_norm.unsqueeze(dim=2), q_norm.unsqueeze(dim=1))  # []
        energy = torch.bmm(proj_query, proj_key)  # transpose check [bn, 3(p123), 3(p123)]
        norm_energy = energy / (nm + 1e-8)
        norm_energy = norm_energy.view(groups, -1)
        mask_cos = torch.sum((norm_energy > delta_cos) + (norm_energy < -delta_cos), 1) > 3  # igonre

        ##ignore padding and invilid depth
        mask_pad = torch.sum(pw[:, 2, :] > self.delta_z, 1) == 3

        ###ignore near
        mask_x = torch.sum(torch.abs(pw_diff[:, 0, :]) < delta_diff, 1) > 0
        mask_y = torch.sum(torch.abs(pw_diff[:, 1, :]) < delta_diff, 1) > 0
        mask_z = torch.sum(torch.abs(pw_diff[:, 2, :]) < delta_diff, 1) > 0

        mask_ignore = (mask_x & mask_y & mask_z) | mask_cos
        mask_near = ~mask_ignore
        mask = mask_pad & mask_near
        return mask, pw

    def normal_from_triplets(self, triplets, simpled_mask):
        triplets = triplets[simpled_mask]
        p12 = triplets[:, :, 1] - triplets[:, :, 0]
        p13 = triplets[:, :, 2] - triplets[:, :, 0]
        normal = torch.cross(p12, p13, dim=1)
        norm = torch.norm(normal, 2, dim=1, keepdim=True)
        valid_mask = norm == 0.0
        valid_mask = valid_mask.to(torch.float32)
        valid_mask *= 0.01
        norm = norm + valid_mask
        normal = normal / norm
        return normal

    def forward(self, pred_depth, gt_masks, gt_planes, gt_depth, k_maritix, select=True):
        C, H, W = pred_depth.shape
        pred_pointcloud = self.transfer_xyz(pred_depth, k_maritix)
        N = gt_planes.shape[0]
        losses = 0
        triplets_num = 0
        nonplanar_mask = torch.logical_not(gt_masks.sum(dim=0).bool())

        for i in range(0, N):
            gt_normal = gt_planes[i]
            pointcloud_seg = pred_pointcloud[gt_masks[i], :]
            num = pointcloud_seg.shape[0]
            p123 = self.select_index(num)
            mask, pw = self.filter_mask(p123, pointcloud_seg)
            dt_normal = self.normal_from_triplets(triplets=pw, simpled_mask=mask)
            cossim = torch.abs(F.cosine_similarity(dt_normal, gt_normal.unsqueeze(dim=0), dim=1))
            loss = 1 - cossim
            if select:
                loss, indices = torch.sort(loss, dim=0, descending=False)
                loss = loss[int(loss.shape[0] * 0.25):]
            loss = torch.nansum(loss) / loss.shape[0]
            losses = losses + loss

        if nonplanar_mask.sum() > 0:
            gt_pointcloud = self.transfer_xyz(gt_depth, k_maritix)
            nonplanar_pointcloud_pred = pred_pointcloud[nonplanar_mask, :]
            nonplanar_pointcloud_gt = gt_pointcloud[nonplanar_mask, :]
            num = nonplanar_pointcloud_gt.shape[0]
            p123 = self.select_index(num)
            mask, pw_gt = self.filter_mask(p123, nonplanar_pointcloud_gt, delta_diff=0.1)
            if mask.sum() == 0:
                return losses / N  # In case no non-planar triplets can be sampled
            pw_pred = self.form_pw_groups(p123, nonplanar_pointcloud_pred)
            pw_pred[pw_pred[:, 2, :] == 0] = 0.0001
            gt_normal = self.normal_from_triplets(triplets=pw_gt, simpled_mask=mask)
            dt_normal = self.normal_from_triplets(triplets=pw_pred, simpled_mask=mask)
            cossim = torch.abs(F.cosine_similarity(dt_normal, gt_normal, dim=1))
            loss = 1 - cossim

            if select:
                loss, indices = torch.sort(loss, dim=0, descending=False)
                loss = loss[int(loss.shape[0] * 0.25):]
            loss = torch.nansum(loss) / loss.shape[0]
            losses = losses + loss
            return losses / (N + 1)
        else:
            return losses / N


class VNL_Loss_ori(nn.Module):
    """
    The original Virtual Normal Loss Function with some modification.
    Since we can't assume that every image are taken by the same camera.
    """

    def __init__(self, input_size,
                 delta_cos=0.867, delta_diff_x=0.01,
                 delta_diff_y=0.01, delta_diff_z=0.01,
                 delta_z=0.0001, sample_ratio=0.15):
        super(VNL_Loss_ori, self).__init__()
        # self.fx = torch.tensor([focal_x], dtype=torch.float32).cuda()
        # self.fy = torch.tensor([focal_y], dtype=torch.float32).cuda()
        self.input_size = input_size
        self.u0 = torch.tensor(input_size[1] // 2, dtype=torch.float32).cuda()
        self.v0 = torch.tensor(input_size[0] // 2, dtype=torch.float32).cuda()
        self.init_image_coor()
        self.delta_cos = delta_cos
        self.delta_diff_x = delta_diff_x
        self.delta_diff_y = delta_diff_y
        self.delta_diff_z = delta_diff_z
        self.delta_z = delta_z
        self.sample_ratio = sample_ratio

    def init_image_coor(self):
        x_row = np.arange(0, self.input_size[1])
        x = np.tile(x_row, (self.input_size[0], 1))
        x = x[np.newaxis, :, :]
        x = x.astype(np.float32)
        x = torch.from_numpy(x.copy()).cuda()
        self.u_u0 = x - self.u0

        y_col = np.arange(0, self.input_size[0])  # y_col = np.arange(0, height)
        y = np.tile(y_col, (self.input_size[1], 1)).T
        y = y[np.newaxis, :, :]
        y = y.astype(np.float32)
        y = torch.from_numpy(y.copy()).cuda()
        self.v_v0 = y - self.v0

    def transfer_xyz(self, depth, fx, fy):
        x = self.u_u0 * torch.abs(depth) / fx
        y = self.v_v0 * torch.abs(depth) / fy
        z = depth
        pw = torch.cat([x, y, z], 1).permute(0, 2, 3, 1)  # [b, h, w, c]
        return pw

    def select_index(self):
        valid_width = self.input_size[1]
        valid_height = self.input_size[0]
        num = valid_width * valid_height
        p1 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p1)
        p2 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p2)
        p3 = np.random.choice(num, int(num * self.sample_ratio), replace=True)
        np.random.shuffle(p3)

        p1_x = p1 % self.input_size[1]
        p1_y = (p1 / self.input_size[1]).astype(np.int)

        p2_x = p2 % self.input_size[1]
        p2_y = (p2 / self.input_size[1]).astype(np.int)

        p3_x = p3 % self.input_size[1]
        p3_y = (p3 / self.input_size[1]).astype(np.int)
        p123 = {'p1_x': p1_x, 'p1_y': p1_y, 'p2_x': p2_x, 'p2_y': p2_y, 'p3_x': p3_x, 'p3_y': p3_y}
        return p123

    def form_pw_groups(self, p123, pw):
        """
        Form 3D points groups, with 3 points in each grouup.
        :param p123: points index
        :param pw: 3D points
        :return:
        """
        p1_x = p123['p1_x']
        p1_y = p123['p1_y']
        p2_x = p123['p2_x']
        p2_y = p123['p2_y']
        p3_x = p123['p3_x']
        p3_y = p123['p3_y']

        pw1 = pw[:, p1_y, p1_x, :]
        pw2 = pw[:, p2_y, p2_x, :]
        pw3 = pw[:, p3_y, p3_x, :]
        # [B, N, 3(x,y,z), 3(p1,p2,p3)]
        pw_groups = torch.cat([pw1[:, :, :, np.newaxis], pw2[:, :, :, np.newaxis], pw3[:, :, :, np.newaxis]], 3)
        return pw_groups

    def filter_mask(self, p123, gt_xyz, delta_cos=0.867,
                    delta_diff_x=0.005,
                    delta_diff_y=0.005,
                    delta_diff_z=0.005):
        pw = self.form_pw_groups(p123, gt_xyz)
        pw12 = pw[:, :, :, 1] - pw[:, :, :, 0]
        pw13 = pw[:, :, :, 2] - pw[:, :, :, 0]
        pw23 = pw[:, :, :, 2] - pw[:, :, :, 1]
        ###ignore linear
        pw_diff = torch.cat([pw12[:, :, :, np.newaxis], pw13[:, :, :, np.newaxis], pw23[:, :, :, np.newaxis]],
                            3)  # [b, n, 3, 3]
        m_batchsize, groups, coords, index = pw_diff.shape
        proj_query = pw_diff.view(m_batchsize * groups, -1, index).permute(0, 2,
                                                                           1)  # (B* X CX(3)) [bn, 3(p123), 3(xyz)]
        proj_key = pw_diff.view(m_batchsize * groups, -1, index)  # B X  (3)*C [bn, 3(xyz), 3(p123)]
        q_norm = proj_query.norm(2, dim=2)
        nm = torch.bmm(q_norm.view(m_batchsize * groups, index, 1), q_norm.view(m_batchsize * groups, 1, index))  # []
        energy = torch.bmm(proj_query, proj_key)  # transpose check [bn, 3(p123), 3(p123)]
        norm_energy = energy / (nm + 1e-8)
        norm_energy = norm_energy.view(m_batchsize * groups, -1)
        mask_cos = torch.sum((norm_energy > delta_cos) + (norm_energy < -delta_cos), 1) > 3  # igonre
        mask_cos = mask_cos.view(m_batchsize, groups)
        ##ignore padding and invilid depth
        mask_pad = torch.sum(pw[:, :, 2, :] > self.delta_z, 2) == 3

        ###ignore near
        mask_x = torch.sum(torch.abs(pw_diff[:, :, 0, :]) < delta_diff_x, 2) > 0
        mask_y = torch.sum(torch.abs(pw_diff[:, :, 1, :]) < delta_diff_y, 2) > 0
        mask_z = torch.sum(torch.abs(pw_diff[:, :, 2, :]) < delta_diff_z, 2) > 0

        mask_ignore = (mask_x & mask_y & mask_z) | mask_cos
        mask_near = ~mask_ignore
        mask = mask_pad & mask_near

        return mask, pw

    def select_points_groups(self, gt_depth, pred_depth, fx, fy):
        pw_gt = self.transfer_xyz(gt_depth, fx, fy)
        pw_pred = self.transfer_xyz(pred_depth, fx, fy)
        B, C, H, W = gt_depth.shape
        p123 = self.select_index()
        # mask:[b, n], pw_groups_gt: [b, n, 3(x,y,z), 3(p1,p2,p3)]
        mask, pw_groups_gt = self.filter_mask(p123, pw_gt,
                                              delta_cos=0.867,
                                              delta_diff_x=0.005,
                                              delta_diff_y=0.005,
                                              delta_diff_z=0.005)

        # [b, n, 3, 3]
        pw_groups_pred = self.form_pw_groups(p123, pw_pred)
        pw_groups_pred[pw_groups_pred[:, :, 2, :] == 0] = 0.0001
        mask_broadcast = mask.repeat(1, 9).reshape(B, 3, 3, -1).permute(0, 3, 1, 2)
        pw_groups_pred_not_ignore = pw_groups_pred[mask_broadcast].reshape(1, -1, 3, 3)
        pw_groups_gt_not_ignore = pw_groups_gt[mask_broadcast].reshape(1, -1, 3, 3)

        return pw_groups_gt_not_ignore, pw_groups_pred_not_ignore

    def forward(self, gt_depth, pred_depth, fx, fy, select=True):
        """
        Virtual normal loss.
        :param pred_depth: predicted depth map, [B,W,H,C]
        :param data: target label, ground truth depth, [B, W, H, C], padding region [padding_up, padding_down]
        :return:
        """
        gt_points, dt_points = self.select_points_groups(gt_depth, pred_depth, fx, fy)

        gt_p12 = gt_points[:, :, :, 1] - gt_points[:, :, :, 0]
        gt_p13 = gt_points[:, :, :, 2] - gt_points[:, :, :, 0]
        dt_p12 = dt_points[:, :, :, 1] - dt_points[:, :, :, 0]
        dt_p13 = dt_points[:, :, :, 2] - dt_points[:, :, :, 0]

        gt_normal = torch.cross(gt_p12, gt_p13, dim=2)
        dt_normal = torch.cross(dt_p12, dt_p13, dim=2)
        dt_norm = torch.norm(dt_normal, 2, dim=2, keepdim=True)
        gt_norm = torch.norm(gt_normal, 2, dim=2, keepdim=True)
        dt_mask = dt_norm == 0.0
        gt_mask = gt_norm == 0.0
        dt_mask = dt_mask.to(torch.float32)
        gt_mask = gt_mask.to(torch.float32)
        dt_mask *= 0.01
        gt_mask *= 0.01
        gt_norm = gt_norm + gt_mask
        dt_norm = dt_norm + dt_mask
        gt_normal = gt_normal / gt_norm
        dt_normal = dt_normal / dt_norm
        loss = torch.abs(gt_normal - dt_normal)
        loss = torch.sum(torch.sum(loss, dim=2), dim=0)
        if select:
            loss, indices = torch.sort(loss, dim=0, descending=False)
            loss = loss[int(loss.size(0) * 0.25):]
        loss = torch.mean(loss)
        return loss
