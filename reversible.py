import torch as th
import torch.nn as nn
import numpy as np
from scipy import interpolate
import matplotlib.pyplot as plt
from matplotlib import cm
from braindecode.torch_ext.util import var_to_np, np_to_var
from braindecode.datautil.iterators import get_balanced_batches


### Reversible model parts

class ReversibleBlock(th.nn.Module):
    def __init__(self, F, G):
        super(ReversibleBlock, self).__init__()
        self.F = F
        self.G = G

    def forward(self, x):
        n_chans = x.size()[1]
        assert n_chans % 2 == 0
        x1 = x[:, :n_chans // 2]
        x2 = x[:, n_chans // 2:]
        y1 = self.F(x1) + x2
        y2 = self.G(y1) + x1
        return th.cat((y1, y2), dim=1)


class SubsampleSplitter(th.nn.Module):
    def __init__(self, stride, chunk_chans_first=True):
        super(SubsampleSplitter, self).__init__()
        if not hasattr(stride, '__len__'):
            stride = (stride, stride)
        self.stride = stride
        self.chunk_chans_first = chunk_chans_first

    def forward(self, x):
        # Chunk chans first to ensure that each of the two streams in the
        # reversible network will see a subsampled version of the whole input
        # (in case the preceding blocks would not alter the input)
        # and not one half of the input
        new_x = []
        if self.chunk_chans_first:
            xs = th.chunk(x, 2, dim=1)
        else:
            xs = [x]
        for one_x in xs:
            for i_stride in range(self.stride[0]):
                for j_stride in range(self.stride[1]):
                    new_x.append(
                        one_x[:, :, i_stride::self.stride[0], j_stride::self.stride[1]])
        new_x = th.cat(new_x, dim=1)
        return new_x


def invert(feature_model, features):
    if feature_model.__class__.__name__ == 'ReversibleBlock' or feature_model.__class__.__name__  == 'SubsampleSplitter':
        feature_model = nn.Sequential(feature_model, )
    for module in reversed(list(feature_model.children())):
        if module.__class__.__name__ == 'ReversibleBlock':
            n_chans = features.size()[1]
            # y1 = self.F(x1) + x2
            # y2 = self.G(y1) + x1
            y1 = features[:, :n_chans // 2]
            y2 = features[:, n_chans // 2:]

            x1 = y2 - module.G(y1)
            x2 = y1 - module.F(x1)
            features = th.cat((x1, x2), dim=1)
        if module.__class__.__name__ == 'SubsampleSplitter':
            # after splitting the input into two along channel dimension if possible
            # for i_stride in range(self.stride):
            #    for j_stride in range(self.stride):
            #        new_x.append(one_x[:,:,i_stride::self.stride, j_stride::self.stride])
            n_all_chans_before = features.size()[1] // (module.stride[0] * module.stride[1])
            # if ther was only one chan before, chunk had no effect
            if module.chunk_chans_first and (n_all_chans_before > 1):
                chan_features = th.chunk(features, 2, dim=1)
            else:
                chan_features = [features]
            all_previous_features = []
            for one_chan_features in chan_features:
                previous_features = th.zeros(one_chan_features.size()[0],
                             one_chan_features.size()[1] // (module.stride[0] * module.stride[1]),
                             one_chan_features.size()[2] * module.stride[0],
                             one_chan_features.size()[3] * module.stride[1])
                if features.is_cuda:
                    previous_features = previous_features.cuda()
                previous_features = th.autograd.Variable(previous_features)

                n_chans_before = previous_features.size()[1]
                cur_chan = 0
                for i_stride in range(module.stride[0]):
                    for j_stride in range(module.stride[1]):
                        previous_features[:, :, i_stride::module.stride[0],
                        j_stride::module.stride[1]] = (
                            one_chan_features[:,
                            cur_chan * n_chans_before:cur_chan * n_chans_before + n_chans_before])
                        cur_chan += 1
                all_previous_features.append(previous_features)
            features = th.cat(all_previous_features, dim=1)
    return features


class GaussianMixtureDensities(th.nn.Module):
    def __init__(self, means_per_dim, stds_per_dim, weights_per_cluster):
        self.means_per_dim = means_per_dim
        self.stds_per_dim = stds_per_dim
        self.weights_per_cluster = weights_per_cluster

        super(GaussianMixtureDensities, self).__init__()

    def forward(self, x):
        log_pdf_per_cluster = log_gaussian_pdf_per_cluster(
            x, self.means_per_dim, self.stds_per_dim)
        log_pdf_per_cluster = log_pdf_per_cluster + th.log(
            self.weights_per_cluster.unsqueeze(0))
        return log_pdf_per_cluster


class GaussianMixtureDistances(th.nn.Module):
    def __init__(self, means_per_dim, stds_per_dim, weights_per_cluster):
        self.means_per_dim = means_per_dim
        self.stds_per_dim = stds_per_dim
        self.weights_per_cluster = weights_per_cluster

        super(GaussianMixtureDistances, self).__init__()

    def forward(self, x):
        samples_per_cluster = []
        for i_cluster in range(self.means_per_dim.size()[0]):
            sizes = [0] * self.means_per_dim.size()[0]
            sizes[i_cluster] = len(x) * 2
            this_samples = sample_mixture_gaussian(
                sizes, self.means_per_dim, self.stds_per_dim)
            samples_per_cluster.append(this_samples)

        samples_per_cluster = th.stack(samples_per_cluster)

        diffs = samples_per_cluster.unsqueeze(3) - x.t().unsqueeze(0).unsqueeze(
            1)
        # cluster x samples x dims x examples
        avg_diff = th.mean(th.mean(diffs * diffs, dim=2), dim=1).t()
        return -avg_diff
        # old version:
        #eps = 1e-6
        #similarity = 1 / (avg_diff + eps)
        #similarity = similarity * self.weights_per_cluster.unsqueeze(0)
        #return similarity


class GaussianMeanDistances(th.nn.Module):
    def __init__(self, means_per_dim, stds_per_dim, weights_per_cluster,
                 normalize_by_std=False):
        self.means_per_dim = means_per_dim
        self.stds_per_dim = stds_per_dim
        self.weights_per_cluster = weights_per_cluster
        self.normalize_by_std = normalize_by_std

        super(GaussianMeanDistances, self).__init__()

    def forward(self, x):
        # x is examples x dims
        diffs_to_mean = x.unsqueeze(2) - self.means_per_dim.t().unsqueeze(0)
        # now examples x dims x clusters
        eps = 1e-6
        if self.normalize_by_std:
            diffs_to_mean = diffs_to_mean / (self.stds_per_dim.t().unsqueeze(0) + eps)
        avg_squared_diffs = th.mean(diffs_to_mean * diffs_to_mean, dim=1)
        return -avg_squared_diffs




def get_inputs_from_reverted_samples(n_inputs, means_per_dim, stds_per_dim,
                                     weights_per_cluster,
                                     feature_model):
    feature_model.eval()
    sizes = sizes_from_weights(n_inputs, var_to_np(weights_per_cluster))
    gauss_samples = sample_mixture_gaussian(sizes, means_per_dim, stds_per_dim)
    rec_var = invert(feature_model, gauss_samples.unsqueeze(2).unsqueeze(3))
    rec_examples = var_to_np(rec_var).squeeze()
    return rec_examples, gauss_samples


def weights_init(module, conv_weight_init_fn):
    classname = module.__class__.__name__
    if (('Conv' in classname) or (
        'Linear' in classname)) and classname != "AvgPool2dWithConv":
        conv_weight_init_fn(module.weight)
        if module.bias is not None:
            th.nn.init.constant(module.bias, 0)
    elif 'BatchNorm' in classname:
        th.nn.init.constant(module.weight, 1)
        th.nn.init.constant(module.bias, 0)


def init_model_params(feature_model, gain):
    feature_model.apply(lambda module: weights_init(
        module,
        lambda w: th.nn.init.xavier_uniform(w, gain=gain)))


## Sampling gaussian mixture

def sample_mixture_gaussian(sizes_per_cluster, means_per_dim, stds_per_dim):
    # assume mean/std are clusters x dims
    parts = []
    n_dims = means_per_dim.size()[1]
    for n_samples, mean, std  in zip(sizes_per_cluster, means_per_dim, stds_per_dim):
        if n_samples == 0: continue
        assert n_samples > 0
        samples = th.randn(n_samples, n_dims)
        samples = th.autograd.Variable(samples)
        if std.is_cuda:
            samples = samples.cuda()
        samples = samples * std.unsqueeze(0) + mean.unsqueeze(0)
        parts.append(samples)
    all_samples = th.cat(parts, dim=0)
    return all_samples


def sizes_from_weights(size, weights, ):
    weights = weights / np.sum(weights)
    fractional_sizes = weights * size

    rounded = np.int64(np.round(fractional_sizes))
    diff_with_half =  (fractional_sizes % 1) - 0.5

    n_total = np.sum(rounded)
    # Those closest to 0.5 rounded, take next biggest or next smallest number
    # to match wanted overall size
    while n_total > size:
        mask = (diff_with_half > 0) & (rounded > 0)
        if np.sum(mask) == 0:
            mask = rounded > 0
        i_min = np.argsort(diff_with_half[mask])[0]
        i_min = np.flatnonzero(mask)[i_min]
        diff_with_half[i_min] += 0.5
        rounded[i_min] -= 1
        n_total -= 1
    while n_total < size:
        mask = (diff_with_half < 0) & (rounded > 0)
        if np.sum(mask) == 0:
            mask = rounded > 0
        i_min = np.argsort(-diff_with_half[mask])[0]
        i_min = np.flatnonzero(mask)[i_min]
        diff_with_half[i_min] -= 0.5
        rounded[i_min] += 1
        n_total += 1

    assert np.sum(rounded) == size
    # pytorch needs list of int
    sizes = [int(s) for s in rounded]
    return sizes


def log_gaussian_pdf_per_cluster(X, means_per_dim, stds_per_dim, eps=1e-6):
    ## Tested with comparison to scipy:
    # i_cluster = 2
    # cov_mat = th.eye(32) * stds_per_dim[i_cluster].data * stds_per_dim[
    #     i_cluster].data
    #
    # dist = scipy.stats.multivariate_normal(var_to_np(means_per_dim[i_cluster]),
    #                                        cov_mat.cpu().numpy())
    #
    # dist.logpdf(var_to_np(X[1]))
    # log (1/sqrt(2*pi)
    subtractors = th.FloatTensor([np.log(2 * np.pi) / 2])
    subtractors, means_per_dim = ensure_on_same_device(
        subtractors, means_per_dim)
    subtractors = th.autograd.Variable(subtractors)
    subtractors = (subtractors * stds_per_dim.size()[1]) + th.sum(th.log(stds_per_dim + eps), dim=1)
    # now subtractors are per cluster

    demeaned_X = X.unsqueeze(2) - means_per_dim.t().unsqueeze(0)
    squared_std = stds_per_dim * stds_per_dim
    # demeaned_X are examples x dims x clusters
    log_pdf_per_dim_per_cluster = (
        -(demeaned_X * demeaned_X) / (2 * squared_std.t().unsqueeze(0)))
    # log_pdf_per_dim_per_cluster are examples x dims x clusters
    log_pdf_per_dim_per_cluster = th.sum(log_pdf_per_dim_per_cluster, dim=1)
    log_pdf_per_cluster = log_pdf_per_dim_per_cluster - subtractors.unsqueeze(0)
    return log_pdf_per_cluster


## Transport/Wasserstein loss

import itertools


def dist_transport_loss(means_per_dim, stds_per_dim):
    assert len(means_per_dim) == 2
    mean_diff = means_per_dim[1] - means_per_dim[0]
    normed_diff = mean_diff / th.norm(mean_diff, p=2)

    _, transformed_stds = transform_gaussian_by_dirs(means_per_dim, stds_per_dim, normed_diff.unsqueeze(0))
    transformed_stds = transformed_stds.squeeze()
    loss = -th.sqrt(th.sum(mean_diff * mean_diff)) + th.sqrt(th.sum(transformed_stds * transformed_stds))
    return loss


def dist_transport_loss_relative(means_per_dim, stds_per_dim, std_offset=1):
    assert len(means_per_dim) == 2
    mean_diff = means_per_dim[1] - means_per_dim[0]
    normed_diff = mean_diff / th.norm(mean_diff, p=2)

    _, transformed_stds = transform_gaussian_by_dirs(means_per_dim, stds_per_dim, normed_diff.unsqueeze(0))
    transformed_stds = transformed_stds.squeeze()
    loss = (th.sum(transformed_stds) + std_offset) / (th.sqrt(th.sum(mean_diff * mean_diff)))
    return loss


def pairwise_projection_loss(X, targets, means_per_dim, stds_per_dim,
                             scaled=True, add_stds=False):
    outs_per_class_pair, midpoints, stds_per_pair = pairwise_projections(X, means_per_dim, stds_per_dim)


    losses = th.autograd.Variable(th.zeros(len(outs_per_class_pair)))
    for i_cluster in range(len(means_per_dim)):
        relevant_outs = outs_per_class_pair[:,i_cluster,:]
        #relevant_outs = th.cat((relevant_outs[:,:i_cluster], relevant_outs[:,i_cluster+1:]), dim=1)
        n_elems = len(th.nonzero(targets==i_cluster))

        relevant_outs = relevant_outs[(targets==i_cluster).unsqueeze(1)].resize(
            n_elems, outs_per_class_pair.size()[2])

        # now examples x clusters
        # shows lengths of examples projected on arrows from wanted cluster to other clusters
        # -> the smaller the better, negative indicates already "behind" the wanted mean
        # loss: if (looking towards unwanted cluster) in front of wanted mean,
        # take squared (difference to wanted mean + mean variance for both), only punish if point
        # + variance is further away from mean than midpoint
        # or midpoint times some scalar (0.25)
        #
        relevant_stds = stds_per_pair[i_cluster]
        relevant_midpoints = midpoints[i_cluster]
        if add_stds:
            relevant_outs = relevant_outs + relevant_stds.unsqueeze(0)
        #scaled_stds = relevant_stds / midpoints
        #with_stds = relevant_outs + relevant_stds.unsqueeze(0)
        if scaled:
            scaled_outs = relevant_outs / (relevant_midpoints.unsqueeze(0) + 1e-6)
        else:
            scaled_outs = relevant_outs
        # scaled outs still examples x clusters
        parts = []
        if i_cluster > 0:
            parts.append(scaled_outs[:,:i_cluster])
        if i_cluster < scaled_outs.size()[1]:
            parts.append(scaled_outs[:,i_cluster:])
        scaled_outs = th.cat(parts, dim=1)
        if scaled:
            outs_to_be_penalized = (scaled_outs > 0.05).type(th.FloatTensor)
        else:
            outs_to_be_penalized = (scaled_outs > 0).type(th.FloatTensor)
        this_losses = th.sum(outs_to_be_penalized * (scaled_outs * scaled_outs),
                             dim=1)
        losses[(targets==i_cluster)] = this_losses
    return losses


def pairwise_projections(X, means_per_dim, stds_per_dim):
    diff_between_clusters = means_per_dim.unsqueeze(
        0) - means_per_dim.unsqueeze(1)
    # clusters x clusters x dims
    # outs ar examples x dims
    outs_per_class_pair = th.sum(
        X.unsqueeze(1).unsqueeze(2) * diff_between_clusters.unsqueeze(0), dim=3)
    # now examples x cluster x cluster
    mean_between_clusters = (means_per_dim.unsqueeze(
        0) + means_per_dim.unsqueeze(1)) / 2
    # cluster x cluster x dims
    midpoints = th.sum(mean_between_clusters * diff_between_clusters, dim=2)

    first_mean_projected = th.sum(
        means_per_dim.unsqueeze(1) * diff_between_clusters, dim=2)
    # clusters x clusters
    outs_per_class_pair = outs_per_class_pair - first_mean_projected.unsqueeze(
        0)
    midpoints = midpoints - first_mean_projected

    stds_per_pair = th.autograd.Variable(
        th.zeros(len(means_per_dim), len(means_per_dim)))

    for i_c_a, i_c_b in itertools.product(range(len(means_per_dim)),
                                          range(len(means_per_dim))):
        if i_c_a == i_c_b:
            continue
            # Somehow otherwise get nans in gradients although
            # not clear to me why...
        diff_vector = diff_between_clusters[i_c_a, i_c_b]
        relevant_means = th.stack((means_per_dim[i_c_a], means_per_dim[i_c_b]),
                                  dim=0)
        normed_diff_vector = diff_vector / th.norm(diff_vector, p=2)
        relevant_stds = th.stack((stds_per_dim[i_c_a], stds_per_dim[i_c_b]),
                                 dim=0)
        _, pair_std = transform_gaussian_by_dirs(relevant_means, relevant_stds,
                                                 normed_diff_vector.unsqueeze(0))
        stds_per_pair[i_c_a, i_c_b] = th.sum(pair_std)

    return outs_per_class_pair, midpoints, stds_per_pair


def pairwise_projection_score(X, means_per_dim, stds_per_dim):
    outs_per_class_pair, midpoints, stds_per_pair = pairwise_projections(X, means_per_dim, stds_per_dim)
    with_stds = outs_per_class_pair + stds_per_pair.unsqueeze(0)
    scaled_outs = with_stds / (midpoints.unsqueeze(0) + 1e-6)
    scores = -th.sqrt(th.sum(scaled_outs * scaled_outs, dim=2))
    return scores


def sample_transport_loss(
        samples, means_per_dim, stds_per_dim, weights_per_cluster, abs_or_square,
        n_interpolation_samples,
        cuda=False, directions=None, backprop_sample_loss_to_cluster_weights=True,
        normalize_by_stds=True, energy_sample_loss=False):
    # common
    if directions is None:
        directions = sample_directions(samples.size()[1], True, cuda=cuda)
    else:
        directions = norm_and_var_directions(directions)

    projected_samples = th.mm(samples, directions.t())
    sorted_samples, _ = th.sort(projected_samples, dim=0)
    if energy_sample_loss:
        sample_loss = sampled_energy_transport_loss(
            projected_samples, directions,
            means_per_dim, stds_per_dim, weights_per_cluster,
            abs_or_square,
            backprop_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_stds=normalize_by_stds)
    else:
        sample_loss =  sampled_transport_diffs_interpolate_sorted_part(
            sorted_samples, directions, means_per_dim,
            stds_per_dim, weights_per_cluster, n_interpolation_samples,
            abs_or_square=abs_or_square,
            backprop_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_stds=normalize_by_stds)
    return sample_loss


def sampled_transport_diffs_interpolate_sorted_part(
        sorted_samples_batch, directions, means_per_dim,
        stds_per_dim, weights_per_cluster, n_interpolation_samples, abs_or_square,
        backprop_to_cluster_weights, normalize_by_stds):
    # sampling based stuff
    sorted_samples_cluster, diff_weights, stds_per_sample = projected_samples_mixture_sorted(
        weights_per_cluster, means_per_dim, stds_per_dim,
        directions, len(sorted_samples_batch),
        n_interpolation_samples=n_interpolation_samples,
        backprop_to_cluster_weights=backprop_to_cluster_weights,
        compute_stds_per_sample=normalize_by_stds)
    diffs = sorted_samples_cluster - sorted_samples_batch
    # examples x directions
    if normalize_by_stds:
        diffs = diffs / stds_per_sample
    else:
        assert stds_per_sample is None

    if abs_or_square == 'abs':
        if backprop_to_cluster_weights:
            sample_loss = th.mean(th.abs(diffs) * diff_weights)
        else:
            sample_loss = th.mean(th.abs(diffs))
    else:
        assert abs_or_square == 'square'
        if backprop_to_cluster_weights:
            sample_loss = th.mean(th.sqrt(th.mean((diffs * diffs) * diff_weights, dim=0)))
        else:
            sample_loss = th.mean(th.sqrt(th.mean(diffs * diffs, dim=0)))
    return sample_loss


def sampled_energy_transport_loss(
        projected_samples, directions,
        means_per_dim, stds_per_dim, weights_per_cluster,
        abs_or_square,
        backprop_to_cluster_weights,
        normalize_by_stds):
    permuted_samples = projected_samples[th.randperm(len(projected_samples))]
    proj_samples_a, proj_samples_b = th.chunk(permuted_samples, 2)
    sorted_samples_a, _ = th.sort(proj_samples_a, dim=0)
    sorted_samples_b, _ = th.sort(proj_samples_b, dim=0)
    sorted_samples_cluster_a, diff_weights_a, stds_per_sample_a = projected_samples_mixture_sorted(
        weights_per_cluster, means_per_dim, stds_per_dim,
        directions, len(sorted_samples_a),
        n_interpolation_samples=len(sorted_samples_a),
        backprop_to_cluster_weights=backprop_to_cluster_weights,
        compute_stds_per_sample=normalize_by_stds)
    eps = 1e-6
    if stds_per_sample_a is not None:
        stds_per_sample_a = th.clamp(stds_per_sample_a, min=eps)
    sorted_samples_cluster_b, diff_weights_b, stds_per_sample_b = projected_samples_mixture_sorted(
        weights_per_cluster, means_per_dim, stds_per_dim,
        directions, len(sorted_samples_b),
        n_interpolation_samples=len(sorted_samples_a),
        backprop_to_cluster_weights=backprop_to_cluster_weights,
        compute_stds_per_sample=normalize_by_stds)
    eps = 1e-6
    if stds_per_sample_b is not None:
        stds_per_sample_b = th.clamp(stds_per_sample_b, min=eps)
    diffs_x_y_a = sorted_samples_a - sorted_samples_cluster_a
    #diffs_x_y_b = sorted_samples_b - sorted_samples_cluster_b
    diffs_x_x = sorted_samples_a - sorted_samples_b
    diffs_y_y = sorted_samples_cluster_a - sorted_samples_cluster_b


    if normalize_by_stds:
        diffs_x_y_a = diffs_x_y_a / stds_per_sample_a
        #diffs_x_y_b = diffs_x_y_b / stds_per_sample_b
        diffs_y_y = diffs_y_y / ((stds_per_sample_a + stds_per_sample_b) / 2)

    if abs_or_square == 'abs':
        diffs_x_x = th.mean(th.abs(diffs_x_x))
        if backprop_to_cluster_weights:
            diffs_x_y_a = th.mean(th.abs(diffs_x_y_a) * diff_weights_a)
            #diffs_x_y_b = th.mean(th.abs(diffs_x_y_b) * diff_weights_b)
            diffs_y_y = th.mean(th.abs(diffs_y_y) * ((diff_weights_a + diff_weights_b) / 2))
        else:
            diffs_x_y_a = th.mean(th.abs(diffs_x_y_a))
            #diffs_x_y_b = th.mean(th.abs(diffs_x_y_b))
            diffs_y_y = th.mean(th.abs(diffs_y_y))
    else:
        assert abs_or_square == 'square'
        diffs_x_x = th.mean((diffs_x_x * diffs_x_x))
        if backprop_to_cluster_weights:
            diffs_x_y_a = th.mean((diffs_x_y_a * diffs_x_y_a) * diff_weights_a)
            #diffs_x_y_b = th.mean((diffs_x_y_b * diffs_x_y_b) * diff_weights_b)
            diffs_y_y = th.mean((diffs_y_y * diffs_y_y) * ((diff_weights_a + diff_weights_b) / 2))
        else:
            diffs_x_y_a = th.mean((diffs_x_y_a * diffs_x_y_a))
            #diffs_x_y_b = th.mean((diffs_x_y_b * diffs_x_y_b))
            diffs_y_y = th.mean((diffs_y_y * diffs_y_y))
    sample_loss = 2 * diffs_x_y_a - diffs_x_x - diffs_y_y
    #sample_loss = diffs_x_y_b + diffs_x_y_a - diffs_x_x - diffs_y_y
    return sample_loss


def projected_samples_mixture_sorted(
        weights_per_cluster, means_per_dim, stds_per_dim,
        directions, n_samples, n_interpolation_samples,
        backprop_to_cluster_weights, compute_stds_per_sample):
    sizes = sizes_from_weights(n_interpolation_samples,
                               var_to_np(weights_per_cluster))
    dir_means, dir_stds = transform_gaussian_by_dirs(means_per_dim,
                                                     stds_per_dim, directions)
    cluster_samples = sample_mixture_gaussian(sizes, dir_means.t(),
                                              dir_stds.t())
    if backprop_to_cluster_weights:
        weights_per_sample = get_weights_per_sample(
            weights_per_cluster /th.sum(weights_per_cluster), sizes)
    sorted_cluster_samples, sort_inds = th.sort(cluster_samples, dim=0)
    if backprop_to_cluster_weights:
        weights_per_sample = th.stack(
            [weights_per_sample[sort_inds[:, i_dim]]
             for i_dim in range(sort_inds.size()[1])],
            dim=1)
    if compute_stds_per_sample:
        # these are std factors per sample, unsorted
        std_factors = []
        for i_cluster, size in enumerate(sizes):
            if size > 0:
                std_factors.append(
                    dir_stds[:, i_cluster:i_cluster + 1].repeat(1, size))
        std_factors = th.cat(std_factors, dim=1)
        # now directions x samples
        std_factors = std_factors.t()
        # now samples x directions
        std_per_sample = th.stack(
            [std_factors[:, i_dim][sort_inds[:, i_dim]]
             for i_dim in range(sort_inds.size()[1])],
            dim=1)

    offset_x_in_input = -0.5 + 0.5 * (
        len(sorted_cluster_samples) / n_samples)
    x_grid = th.linspace(offset_x_in_input,
                         len(sorted_cluster_samples) - 1 - offset_x_in_input,
                         n_samples)
    i_low = th.floor(x_grid)
    i_high = th.ceil(x_grid)
    weights_high = x_grid - i_low
    i_low = th.clamp(i_low, min=0)
    i_high = th.clamp(i_high, max=n_interpolation_samples-1)
    i_low = i_low.type(th.LongTensor)
    i_high = i_high.type(th.LongTensor)
    weights_high = th.autograd.Variable(weights_high)
    i_low, i_high, sorted_cluster_samples, weights_high = ensure_on_same_device(
        i_low, i_high, sorted_cluster_samples, weights_high)
    vals_low = sorted_cluster_samples[i_low]
    vals_high = sorted_cluster_samples[i_high]
    vals_interpolated = (vals_low * (1 - weights_high).unsqueeze(
        1)) + (vals_high * weights_high.unsqueeze(1))
    if backprop_to_cluster_weights:
        weights_per_sample = (weights_per_sample[i_low] * (1 - weights_high).unsqueeze(1) +
                              weights_per_sample[i_high] * weights_high.unsqueeze(1))
    else:
        weights_per_sample = None
    if compute_stds_per_sample:
        std_per_sample = (std_per_sample[i_low] * (1 - weights_high).unsqueeze(1) +
                              std_per_sample[i_high] * weights_high.unsqueeze(1))
    else:
        std_per_sample = None
    return vals_interpolated, weights_per_sample, std_per_sample


def get_weights_per_sample(weights_per_cluster, sizes):
    # weights will be all one in the end
    # however they are normalized by a number that is not
    # backpropagated through, so there will be an appropriate gradient
    # through them
    all_weights = []
    for i_cluster in range(len(sizes)):
        size = sizes[i_cluster]
        weight = weights_per_cluster[i_cluster]
        if size == 0:
            continue
        assert size > 0
        np_weight = float(var_to_np(weight))
        assert np_weight >= 0
        if np_weight > 0:
            new_weight = weight / np_weight
        else:
            new_weight = weight # should be 0
        all_weights.append(new_weight.expand(size))
    return th.cat(all_weights)


def sample_directions(n_dims, orthogonalize, cuda):
    directions = th.randn(n_dims, n_dims)
    if orthogonalize:
        directions, _ = th.qr(directions)

    if cuda:
        directions = directions.cuda()
    directions = th.autograd.Variable(directions, requires_grad=False)
    norm_factors = th.norm(directions, p=2, dim=1, keepdim=True)
    directions = directions / norm_factors
    return directions


def norm_and_var_directions(directions):
    if th.is_tensor(directions):
        directions = th.autograd.Variable(directions, requires_grad=False)
    norm_factors = th.norm(directions, p=2, dim=1, keepdim=True)
    directions = directions / norm_factors
    return directions


def transform_gaussian_by_dirs(means, stds, directions):
    # directions is directions x dims
    # means is clusters x dims
    # stds is clusters x dims
    transformed_means = th.mm(means, directions.transpose(1, 0)).transpose(1, 0)
    # transformed_means is now
    # directions x clusters
    stds_for_dirs = stds.transpose(1, 0).unsqueeze(0)  # 1 x dims x clusters
    transformed_stds = th.sqrt(th.sum(
        (directions * directions).unsqueeze(2) *
        (stds_for_dirs * stds_for_dirs),
        dim=1))
    # transformed_stds is now
    # directions x clusters
    return transformed_means, transformed_stds


def ensure_on_same_device(*variables):
    any_cuda = np.any([v.is_cuda for v in variables])
    if any_cuda:
        variables = [ensure_cuda(v) for v in variables]
    return variables


def ensure_cuda(v):
    if not v.is_cuda:
        v = v.cuda()
    return v


def get_exact_size_batches(n_trials, rng, batch_size):
    i_trials = np.arange(n_trials)
    rng.shuffle(i_trials)
    i_trial = 0
    batches = []
    for i_trial in range(0, n_trials-batch_size, batch_size):
        batches.append(i_trials[i_trial: i_trial+batch_size])
    i_trial = i_trial + batch_size

    last_batch = i_trials[i_trial:]
    n_remain = batch_size - len(last_batch)
    last_batch = np.concatenate((last_batch, i_trials[:n_remain]))
    batches.append(last_batch)
    return batches


def get_batches_equal_classes(targets, n_classes, rng, batch_size):
    batches_per_cluster = []
    for i_cluster in range(n_classes):
        n_examples = np.sum(targets == i_cluster)
        examples_per_batch = get_exact_size_batches(
            n_examples, rng, batch_size)
        this_cluster_indices = np.nonzero(targets == i_cluster)[0]
        examples_per_batch = [this_cluster_indices[b] for b in examples_per_batch]
        # revert back to actual indices
        batches_per_cluster.append(examples_per_batch)

    batches = np.concatenate(batches_per_cluster, axis=1)
    return batches


def train_epoch_per_class(
        inputs, batch_size, rng,
        feature_model,
        means_per_dim, stds_per_dim, weights_per_cluster,
        directions_adv,
        optimizer, optimizer_adv,
        std_l1, mean_l1, weight_l1,
        clf, targets, loss_function,
        unlabelled_inputs,
        unlabeled_cluster_weights,
        optim_unlabeled,
        normalize_by_std):
    feature_model.train()
    all_trans_losses = []
    all_l1_losses = []
    all_target_losses = []
    all_losses = []
    examples_per_batch = get_batches_equal_classes(var_to_np(targets), 2, rng, batch_size)
    for i_examples in examples_per_batch:
        #print("weights_per_cluster epoch", weights_per_cluster)
        batch_X = inputs[th.LongTensor(i_examples)]
        batch_y = targets[th.LongTensor(i_examples)]
        (trans_loss, threshold_l1_penalty, target_loss,
         total_loss) = train_on_batch_per_class(
                batch_X, feature_model, means_per_dim,
                stds_per_dim, weights_per_cluster,
                directions_adv, optimizer, optimizer_adv,
                std_l1=std_l1, mean_l1=mean_l1, weight_l1=weight_l1,
                clf=clf, batch_y=batch_y, loss_function=loss_function,
                add_mean_diff_directions=True,
                unlabelled_inputs=unlabelled_inputs,
                unlabeled_cluster_weights=unlabeled_cluster_weights,
                optim_unlabeled=optim_unlabeled,
                normalize_by_std=normalize_by_std)
        all_trans_losses.append(var_to_np(trans_loss))
        all_l1_losses.append(var_to_np(threshold_l1_penalty))
        all_target_losses.append(var_to_np(target_loss))
        all_losses.append(var_to_np(total_loss))
    return all_trans_losses, all_l1_losses, all_target_losses, all_losses


def train_on_batch_per_class(
        batch_X, feature_model,
        means_per_dim, stds_per_dim, weights_per_cluster,
        directions_adv, optimizer, optimizer_adv,
        std_l1, mean_l1, weight_l1, clf, batch_y, loss_function,
        add_mean_diff_directions=True,
        unlabelled_inputs=None, unlabeled_cluster_weights=None,
        optim_unlabeled=None, normalize_by_std=False):
    assert batch_y is not None
    assert len(batch_X) == len(batch_y)
    if list(feature_model.parameters())[0].is_cuda:
        batch_X = batch_X.cuda()
        batch_y = batch_y.cuda()
        if unlabelled_inputs is not None:
            unlabelled_inputs = unlabelled_inputs.cuda()
    batch_outs = feature_model(batch_X).squeeze()
    if unlabelled_inputs is not None:
        unlabeled_outs = feature_model(unlabelled_inputs).squeeze()
    else:
        unlabeled_outs = None
    trans_loss = compute_class_trans_loss(
        batch_outs,
        means_per_dim, stds_per_dim,
        directions_adv, batch_y,
        add_mean_diff_directions=add_mean_diff_directions,
        unlabeled_outs=unlabeled_outs,
        unlabeled_cluster_weights=unlabeled_cluster_weights,
        normalize_by_std=normalize_by_std)

    threshold_l1_penalty = ((th.mean(th.abs(stds_per_dim))) * std_l1 +
                                th.mean(th.abs(weights_per_cluster)) * weight_l1 +
                                th.mean(th.abs(means_per_dim)) * mean_l1)

    assert batch_y is not None
    if clf is not None:
        preds = clf(batch_outs)
    else:
        preds = batch_outs
    target_loss = loss_function(preds, batch_y)

    total_loss = trans_loss + threshold_l1_penalty + target_loss
    if optim_unlabeled is not None:
        optim_unlabeled.zero_grad()
    optimizer.zero_grad()
    optimizer_adv.zero_grad()
    total_loss.backward()
    optimizer.step()
    if optim_unlabeled is not None:
        optim_unlabeled.step()
    # directions should try to increase loss.
    directions_adv.grad.data.neg_()
    optimizer_adv.step()
    weights_per_cluster.data.clamp_(min=0)
    weights_per_cluster.data.div_(th.sum(weights_per_cluster.data))
    #stds_per_dim.data.clamp_(min=1e-4)

    return trans_loss, threshold_l1_penalty, target_loss, total_loss


def compute_class_trans_loss(
        batch_outs,
        means_per_dim, stds_per_dim,
        directions_adv, batch_y,
        add_mean_diff_directions=True,
        unlabeled_outs=None,
        unlabeled_cluster_weights=None,
        normalize_by_std=False):
    if batch_y is not None:
        assert len(batch_outs) == len(batch_y)
    trans_losses = []
    if add_mean_diff_directions:
        assert len(means_per_dim) == 2
        mean_diff = means_per_dim[1] - means_per_dim[0]
    dirs = [None, None, norm_and_var_directions(directions_adv)]
    # HACKHACK!!
    if batch_y is None:
        dirs = [mean_diff.unsqueeze(0)]
    for a_dir in dirs:
        if a_dir is None:
            a_dir = sample_directions(
                means_per_dim.size()[1], orthogonalize=True,
                cuda=means_per_dim.is_cuda)
        if add_mean_diff_directions:
            a_dir = th.cat((a_dir, mean_diff.unsqueeze(0)), dim=0)
        trans_loss = transport_loss_per_class(
            batch_outs, means_per_dim, stds_per_dim, batch_y, directions=a_dir,
            unlabeled_samples=unlabeled_outs,
            unlabeled_cluster_weights=unlabeled_cluster_weights,
            normalize_by_std=normalize_by_std)
        trans_losses.append(trans_loss)
    trans_loss = th.sum(th.cat(trans_losses))
    return trans_loss


def transport_loss_per_class(
        samples, means_per_dim, stds_per_dim,
        targets, directions=None, cuda=False, unlabeled_samples=None,
        unlabeled_cluster_weights=None,
        normalize_by_std=False):
    if directions is None:
        directions = sample_directions(samples.size()[1], True, cuda=cuda)
    else:
        directions = norm_and_var_directions(directions)
    n_clusters = len(means_per_dim)
    assert n_clusters == 2

    if unlabeled_samples is not None:
        all_samples  = th.cat((samples, unlabeled_samples), dim=0)
    else:
        all_samples = samples

    projected_samples = th.mm(all_samples, directions.t())
    transformed_means, transformed_stds = transform_gaussian_by_dirs(
        means_per_dim, stds_per_dim, directions)
    weights_per_labeled_sample = th.ones(len(samples))
    weights_per_labeled_sample, samples = ensure_on_same_device(
        weights_per_labeled_sample, samples)
    weights_per_labeled_sample = th.autograd.Variable(weights_per_labeled_sample)
    # directions x clusters
    if unlabeled_samples is not None:
        normed_unlabeled_cluster_weights = unlabeled_cluster_weights
        normed_unlabeled_cluster_weights = normed_unlabeled_cluster_weights / (
            (th.mean(normed_unlabeled_cluster_weights) * 2))
        normed_unlabeled_cluster_weights = th.clamp(normed_unlabeled_cluster_weights,
                                                    min=0.01, max=0.99)
        targets_unlabelled = th.bernoulli(normed_unlabeled_cluster_weights)
        targets, targets_unlabelled = ensure_on_same_device(
            targets, targets_unlabelled)
        # for multiplication keep targets_unlabelled as float

        weights_per_unlabelled_sample = (targets_unlabelled * (normed_unlabeled_cluster_weights + 1e-6) +
                                     (1- targets_unlabelled) * (1 - normed_unlabeled_cluster_weights + 1e-6))
        weights_per_unlabelled_sample = weights_per_unlabelled_sample / th.autograd.Variable(weights_per_unlabelled_sample.data)
        weights_per_sample = th.cat((weights_per_labeled_sample, weights_per_unlabelled_sample), dim=0)
        # for later selection, make targets into long
        targets_unlabelled = targets_unlabelled.type(th.LongTensor)
        targets, targets_unlabelled = ensure_on_same_device(
            targets, targets_unlabelled)
        all_targets = th.cat((targets, targets_unlabelled), dim=0)
    else:
        all_targets = targets
        weights_per_sample = weights_per_labeled_sample

    loss = 0
    for i_cluster in range(n_clusters):
        this_samples = projected_samples[
            (all_targets == i_cluster).unsqueeze(1)].view(-1, len(directions))
        this_diff_weights = weights_per_sample[(all_targets == i_cluster)]
        # now examples x directions
        sorted_samples, i_sorted = th.sort(this_samples, dim=0)
        all_diff_weights = th.stack(
                [this_diff_weights[i_sorted[:, i_dim]]
                 for i_dim in range(i_sorted.size()[1])],
                dim=1)
        # still examples x directions
        this_transformed_means = transformed_means[:,
                                 i_cluster:i_cluster + 1]
        this_transformed_stds = transformed_stds[:, i_cluster:i_cluster + 1]
        n_samples = len(this_samples)
        empirical_cdf = th.linspace(1 / (n_samples), 1 - (1 / (n_samples)),
                                    n_samples).unsqueeze(0)
        # see https://en.wikipedia.org/wiki/Normal_distribution -> Quantile function
        i_cdf = th.FloatTensor([np.sqrt(2.0)]) * th.erfinv(
            2 * empirical_cdf - 1)
        i_cdf = i_cdf.squeeze()
        i_cdf = th.autograd.Variable(i_cdf)
        i_cdf, this_transformed_means = ensure_on_same_device(i_cdf,
                                                              this_transformed_means)

        all_i_cdfs = i_cdf.unsqueeze(
            1) * this_transformed_stds.t() + this_transformed_means.t()
        diffs = all_i_cdfs - sorted_samples
        # diffs are examples x directions
        if normalize_by_std == True:
            eps = 1e-6
            diffs = diffs / this_transformed_stds.t().clamp(min=eps)
            loss = th.sqrt(th.mean(diffs * diffs * all_diff_weights)) + loss
        elif normalize_by_std == 'both':
            eps = 1e-6
            # first wihtout normalization, then with normalization
            loss = th.sqrt(th.mean(diffs * diffs * all_diff_weights)) + loss
            diffs = diffs / this_transformed_stds.t().clamp(min=eps)
            loss = th.sqrt(th.mean(diffs * diffs * all_diff_weights)) + loss
        else:
            assert normalize_by_std == False
            loss = th.sqrt(th.mean(diffs * diffs * all_diff_weights)) + loss

    loss = loss / n_clusters
    return loss


def icdf_grad(p, sigma):
    sqrt_2 = float(np.sqrt(2))
    return sigma * sqrt_2 * 2 * erfinv_grad(2*p - 1)


def erfinv_grad(p):
    sqrt_pi = float(np.sqrt(np.pi))
    erfinved_p = th.erfinv(p)
    return 0.5 * sqrt_pi * th.exp(erfinved_p * erfinved_p)


def compute_icdf_grad_accuracy(outs, targets, means_per_dim, stds_per_dim):
    assert len(outs) == len(targets), "Should have same number of outputs as targets"
    distances = compute_icdf_grads_to_mean(outs, means_per_dim, stds_per_dim)
    assert len(distances) == len(targets)
    return np.mean(np.argmin(var_to_np(distances), axis=1) == var_to_np(targets))


def compute_icdf_grads_to_mean(outs, means_per_dim, stds_per_dim):
    diffs_to_means = outs.unsqueeze(1) - means_per_dim.unsqueeze(0)
    cluster_stds = th.cat(
        [transform_gaussian_by_dirs(means_per_dim[i_cluster:i_cluster + 1],
                                    stds_per_dim[i_cluster:i_cluster + 1],
                                    diffs_to_means[:, i_cluster])[1]
         for i_cluster in range(means_per_dim.size()[0])], dim=1)

    # cluster stds examples x clusters
    x_euclid_diff = th.sqrt(th.sum(diffs_to_means * diffs_to_means, dim=2))
    # examples x clusters
    cdfs = 0.5 * (1 + th.erf(x_euclid_diff / (cluster_stds * np.sqrt(2))))
    # cdfs examples x clusters
    distances = icdf_grad(cdfs - 1e-7, cluster_stds)
    return distances


def compute_icdf_grad_loss(outs, targets, means_per_dim, stds_per_dim):
    distances = compute_icdf_grads_to_mean(outs, means_per_dim, stds_per_dim)
    distances = distances / th.mean(distances, dim=1, keepdim=True)
    loss = 0
    for i_cluster in range(len(means_per_dim)):
        relevant_distances = distances[:,i_cluster]
        loss = loss + th.mean(relevant_distances[targets == i_cluster])
    return loss


class OptimizerUnlabelled(object):
    def __init__(self, unlabeled_cluster_weights, lr=10, alpha=0.1,
                 always_accumulate=False):
        self.unlabeled_cluster_weights = unlabeled_cluster_weights
        self.grad_hist_unlabeled = th.zeros(len(unlabeled_cluster_weights),
                                            2)
        if self.unlabeled_cluster_weights.is_cuda:
            self.grad_hist_unlabeled = self.grad_hist_unlabeled.cuda()
        self.lr = lr
        self.alpha = alpha
        self.always_accumulate = always_accumulate

    def zero_grad(self):
        if self.unlabeled_cluster_weights.grad is not None:
            self.unlabeled_cluster_weights.grad.data.zero_()

    def step(self):
        if self.always_accumulate:
            gradient_unlabeled = th.zeros(len(self.grad_hist_unlabeled))
            gradient_unlabeled, self.grad_hist_unlabeled = ensure_on_same_device(
                gradient_unlabeled, self.grad_hist_unlabeled)
        neg_grad_labels = self.grad_hist_unlabeled[:, 0]
        mask = self.unlabeled_cluster_weights.grad.data > 0
        relevant_neg = neg_grad_labels[mask]
        relevant_neg = (1 - self.alpha) * relevant_neg + (
            self.alpha * th.abs(self.unlabeled_cluster_weights.grad.data[mask]))
        neg_grad_labels[mask] = relevant_neg
        if self.always_accumulate:
            other_grads = self.grad_hist_unlabeled[:, 1]
            gradient_unlabeled[mask] = th.abs(self.unlabeled_cluster_weights.grad.data[mask]) - other_grads[mask]

        pos_grad_labels = self.grad_hist_unlabeled[:, 1]
        mask = self.unlabeled_cluster_weights.grad.data < 0
        relevant_pos = pos_grad_labels[mask]
        relevant_pos = (1 - self.alpha) * relevant_pos + (
            self.alpha * th.abs(self.unlabeled_cluster_weights.grad.data[mask]))
        pos_grad_labels[mask] = relevant_pos
        if self.always_accumulate:
            other_grads = self.grad_hist_unlabeled[:, 0]
            gradient_unlabeled[mask] = -th.abs(self.unlabeled_cluster_weights.grad.data[mask]) + other_grads[mask]

        if not self.always_accumulate:
            gradient_unlabeled = self.grad_hist_unlabeled[:,
                             0] - self.grad_hist_unlabeled[:, 1]
        self.unlabeled_cluster_weights.data = self.unlabeled_cluster_weights.data - self.lr * gradient_unlabeled
        self.unlabeled_cluster_weights.data = self.unlabeled_cluster_weights.data / (
            (th.mean(self.unlabeled_cluster_weights.data, dim=0,
                     keepdim=True)) * 2)
        self.unlabeled_cluster_weights.data = th.clamp(
            self.unlabeled_cluster_weights.data, min=0.01, max=0.99)


def train_epoch(
        inputs, batch_size, rng,
        feature_model,
        means_per_dim, stds_per_dim, weights_per_cluster,
        directions_adv,
        optimizer, optimizer_adv,
        std_l1=0.5, mean_l1=0.01, weight_l1=0,
        backprop_sample_loss_to_cluster_weights=False,
        normalize_by_std=False,
        energy_based=False, clf=None, targets=None,
        loss_function=None):
    feature_model.train()
    all_trans_losses = []
    all_l1_losses = []
    all_target_losses = []
    all_losses = []
    #if energy_based:
    examples_per_batch = get_exact_size_batches(len(inputs), rng, batch_size)
    #else:
    #    examples_per_batch = get_balanced_batches(len(inputs), rng, shuffle=True,
    #                                       batch_size=batch_size)
    for i_examples in examples_per_batch:
        #print("weights_per_cluster epoch", weights_per_cluster)
        batch_X = inputs[th.LongTensor(i_examples)]
        if targets is not None:
            batch_y = targets[th.LongTensor(i_examples)]
        else:
            batch_y = None
        trans_loss, threshold_l1_penalty, target_loss, total_loss = train_on_batch(
            batch_X, feature_model,
            means_per_dim, stds_per_dim, weights_per_cluster,
            directions_adv,
            optimizer, optimizer_adv,
            std_l1=std_l1, mean_l1=mean_l1, weight_l1=weight_l1,
            backprop_sample_loss_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_std=normalize_by_std,
            energy_based=energy_based, clf=clf, targets=batch_y,
            loss_function=loss_function)
        all_trans_losses.append(var_to_np(trans_loss))
        all_l1_losses.append(var_to_np(threshold_l1_penalty))
        if targets is not None:
            all_target_losses.append(var_to_np(target_loss))
        else:
            all_target_losses.append(0)
        all_losses.append(var_to_np(total_loss))
    return all_trans_losses, all_l1_losses, all_target_losses, all_losses


def train_on_batch(
        batch_X, feature_model,
        means_per_dim, stds_per_dim, weights_per_cluster,
        directions_adv,
            optimizer, optimizer_adv,
        std_l1, mean_l1, weight_l1,
        backprop_sample_loss_to_cluster_weights,
        normalize_by_std,
        energy_based, clf, targets, loss_function):
    batch_outs = feature_model(batch_X).squeeze()
    trans_losses = []
    for a_dir in [None, None, norm_and_var_directions(directions_adv)]:
        trans_loss = sample_transport_loss(
            batch_outs, means_per_dim, stds_per_dim,
            weights_per_cluster / th.sum(weights_per_cluster),
            'square', n_interpolation_samples=len(batch_outs) * 2,
            backprop_sample_loss_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_stds=normalize_by_std, energy_sample_loss=energy_based,
            directions=a_dir)
        trans_losses.append(trans_loss)
    trans_loss = th.sum(th.cat(trans_losses))

    threshold_l1_penalty = ((th.mean(th.abs(stds_per_dim))) * std_l1 +
                            th.mean(th.abs(weights_per_cluster)) * weight_l1 +
                            th.mean(th.abs(means_per_dim)) * mean_l1)
    if targets is not None:
        if clf is not None:
            preds = clf(batch_outs)
        else:
            preds = batch_outs
        target_loss = loss_function(preds, targets)
    else:
        target_loss = 0

    total_loss = trans_loss + threshold_l1_penalty + target_loss
    optimizer.zero_grad()
    optimizer_adv.zero_grad()
    total_loss.backward()
    optimizer.step()
    # directions should try to increase loss.
    directions_adv.grad.data.neg_()
    optimizer_adv.step()
    weights_per_cluster.data.clamp_(min=0)
    weights_per_cluster.data.div_(th.sum(weights_per_cluster.data))
    stds_per_dim.data.clamp_(min=1e-4)

    return trans_loss, threshold_l1_penalty, target_loss, total_loss


def eval_inputs(batch_X, feature_model,
        means_per_dim, stds_per_dim, weights_per_cluster,
        directions_adv, energy_based=False):
    all_outs = feature_model(batch_X).squeeze()
    trans_losses = []
    for a_dir in [None, None, norm_and_var_directions(directions_adv)]:
        trans_loss = sample_transport_loss(
            all_outs, means_per_dim, stds_per_dim,
            weights_per_cluster / th.sum(weights_per_cluster),
            'square', n_interpolation_samples=len(all_outs) * 2,
            backprop_sample_loss_to_cluster_weights=False,
            normalize_by_stds=False,
            directions=a_dir, energy_sample_loss=energy_based)
        trans_losses.append(trans_loss)
    return var_to_np(th.cat(trans_losses))


def w2_for_dist_w2_normalized_for_model(outs, directions, soft_targets,
                                        means_per_dim, stds_per_dim):
    projected_samples = th.mm(outs, directions.t())
    loss = 0
    for i_cluster in range(len(means_per_dim)):
        this_means = means_per_dim[i_cluster:i_cluster + 1]
        this_stds = stds_per_dim[i_cluster:i_cluster + 1]
        this_weights = soft_targets[:, i_cluster]
        transformed_means, transformed_stds = transform_gaussian_by_dirs(
            this_means, th.abs(this_stds), directions)
        sorted_samples, i_sorted = th.sort(projected_samples, dim=0)
        sorted_weights = this_weights[i_sorted[:, 0]]
        n_virtual_samples = th.sum(sorted_weights)
        start = 1 / (n_virtual_samples)
        wanted_sum = 1 - (2 / (n_virtual_samples))
        probs = sorted_weights * wanted_sum / n_virtual_samples
        empirical_cdf = start + th.cumsum(probs, dim=0)

        # see https://en.wikipedia.orsorted_softmaxedg/wiki/Normal_distribution -> Quantile function
        i_cdf = th.autograd.Variable(
            th.FloatTensor([np.sqrt(2.0)])) * th.erfinv(
            2 * empirical_cdf - 1)
        i_cdf = i_cdf.squeeze()
        all_i_cdfs = i_cdf.unsqueeze(
            1) * transformed_stds.t() + transformed_means.t()

        diffs = th.autograd.Variable(all_i_cdfs.data) - sorted_samples
        eps = 1e-8
        diffs = diffs / th.autograd.Variable(transformed_stds.data).t().clamp(
            min=eps)
        loss_model = th.sqrt(
            th.mean(th.mean(diffs * diffs, dim=1) * sorted_weights, dim=0))

        # loss b, for mean/std
        diffs = all_i_cdfs - th.autograd.Variable(sorted_samples.data)
        loss_distribution = th.sqrt(
            th.mean(th.mean(diffs * diffs, dim=1) * sorted_weights, dim=0))
        loss = loss + loss_model + loss_distribution

    return loss

## Earlier stuff for Cramer distance/L2-distance of cumulative distribution functions

def analytical_l2_cdf_and_sample_transport_loss(
        samples, means_per_dim, stds_per_dim, weights_per_cluster, abs_or_square,
        n_interpolation_samples,
        cuda=False, directions=None, backprop_sample_loss_to_cluster_weights=True,
        normalize_by_stds=True, energy_sample_loss=False):
    # common
    if directions is None:
        directions = sample_directions(samples.size()[1], True, cuda=cuda)
    else:
        directions = norm_and_var_directions(directions)

    projected_samples = th.mm(samples, directions.t())
    sorted_samples, _ = th.sort(projected_samples, dim=0)
    cdf_loss =  analytical_l2_cdf_loss_given_sorted_samples(
        sorted_samples, directions,
        means_per_dim, stds_per_dim, weights_per_cluster)
    if energy_sample_loss:
        sample_loss = sampled_energy_transport_loss(
            projected_samples, directions,
            means_per_dim, stds_per_dim, weights_per_cluster,
            abs_or_square,
            backprop_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_stds=normalize_by_stds)
    else:
        sample_loss =  sampled_transport_diffs_interpolate_sorted_part(
            sorted_samples, directions, means_per_dim,
            stds_per_dim, weights_per_cluster, n_interpolation_samples,
            abs_or_square=abs_or_square,
            backprop_to_cluster_weights=backprop_sample_loss_to_cluster_weights,
            normalize_by_stds=normalize_by_stds)
    return cdf_loss, sample_loss

def analytical_l2_cdf_loss(
        samples, means_per_dim, stds_per_dim, weights_per_cluster,
        cuda=False, directions=None, ):
    # common
    if directions is None:
        directions = sample_directions(samples.size()[1], True, cuda=cuda)
    else:
        directions = norm_and_var_directions(directions)

    projected_samples = th.mm(samples, directions.t())
    sorted_samples, _ = th.sort(projected_samples, dim=0)
    cdf_loss = analytical_l2_cdf_loss_given_sorted_samples(
        sorted_samples, directions,
        means_per_dim, stds_per_dim, weights_per_cluster)
    return cdf_loss

def analytical_l2_cdf_loss_given_sorted_samples(
        sorted_samples_batch, directions,
        means_per_dim, stds_per_dim, weights_per_cluster):
    n_samples = len(sorted_samples_batch)
    mean_dirs, std_dirs = transform_gaussian_by_dirs(
        means_per_dim, stds_per_dim, directions)
    assert (th.sum(weights_per_cluster) >= 0).data.all()
    normed_weights = weights_per_cluster / th.sum(weights_per_cluster)
    analytical_cdf = multi_directions_gaussian_cdfs(sorted_samples_batch.t(),
                                                    mean_dirs, std_dirs,
                                                    normed_weights)
    #empirical_cdf = th.linspace(1 / (n_samples + 1), 1 - (1 / (n_samples+1)),
    #                            n_samples).unsqueeze(0)
    empirical_cdf = th.linspace(1 / (n_samples), 1 - (1 / (n_samples)),
                                n_samples).unsqueeze(0)
    empirical_cdf = th.autograd.Variable(empirical_cdf)
    directions, empirical_cdf = ensure_on_same_device(directions, empirical_cdf)
    diffs = analytical_cdf - empirical_cdf
    cdf_loss = th.mean(th.sqrt(th.sum(diffs * diffs, dim=1)))
    #cdf_loss = th.mean(th.sqrt(th.mean(diffs * diffs, dim=1)))
    return cdf_loss




def plot_xyz(x, y, z):
    fig = plt.figure(figsize=(5, 5))
    ax = plt.gca()
    xx = np.linspace(min(x), max(x), 100)
    yy = np.linspace(min(y), max(y), 100)
    f = interpolate.NearestNDInterpolator(list(zip(x, y)), z)
    assert len(xx) == len(yy)
    zz = np.ones((len(xx), len(yy)))
    for i_x in range(len(xx)):
        for i_y in range(len(yy)):
            # somehow this is correct. don't know why :(
            zz[i_y, i_x] = f(xx[i_x], yy[i_y])
    assert not np.any(np.isnan(zz))

    ax.imshow(zz, vmin=-np.max(np.abs(z)), vmax=np.max(np.abs(z)), cmap=cm.PRGn,
              extent=[min(x), max(x), min(y), max(y)], origin='lower',
              interpolation='nearest', aspect='auto')

def multi_directions_gaussian_cdfs(x, means, stds, weights):
    # see https://stats.stackexchange.com/questions/187828/how-are-the-error-function-and-standard-normal-distribution-function-related
    # and https://en.wikipedia.org/wiki/Normal_distribution#Cumulative_distribution_function
    # assuming input x is directions x 1-dimensional points
    # assuming means/stds are directions x clusters
    # weights is 1-dimension (number of clusters)
    eps = 1e-6
    stds = th.clamp(stds, min=eps)
    weights = weights / th.sum(weights)
    cdfs = 0.5 * (1 +
                  th.erf((x.unsqueeze(2) - means.unsqueeze(1)) / (
                      stds.unsqueeze(1) * np.sqrt(2))))
    # directions x points x clusters
    cdf = th.sum(cdfs * weights.unsqueeze(0).unsqueeze(1), dim=2)
    # directions x points
    return cdf

