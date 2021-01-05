import logging
from typing import Optional

from tkge.common.configurable import Configurable
from tkge.common.registry import Registrable
from tkge.common.config import Config
from tkge.common.error import ConfigurationError
from tkge.data.dataset import DatasetProcessor
from tkge.indexing import where_in

import torch
import numba

SLOTS = [0, 1, 2, 3]
SLOT_STR = ["s", "p", "o", "t"]
S, P, O, T = SLOTS


class NegativeSampler(Registrable):
    def __init__(self, config: Config, dataset: DatasetProcessor, as_matrix: bool = True):
        super(NegativeSampler, self).__init__(config, configuration_key="negative_sampling")

        self.num_samples = self.config.get("negative_sampling.num_samples")
        self.filter = self.config.get("negative_sampling.filter")
        self.as_matrix = as_matrix

        self.dataset = dataset

    @staticmethod
    def create(config: Config, dataset: DatasetProcessor):
        """Factory method for loss creation"""

        ns_type = config.get("negative_sampling.name")

        if ns_type in NegativeSampler.list_available():
            as_matrix = config.get("negative_sampling.as_matrix")
            kwargs = config.get("model.args")  # TODO: 需要改成key的格式
            return NegativeSampler.by_name(ns_type)(config, dataset, as_matrix)
        else:
            raise ConfigurationError(
                f"{ns_type} specified in configuration file is not supported"
                f"implement your negative samping class with `NegativeSampler.register(name)"
            )

    def _sample(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        raise NotImplementedError

    def _label(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        raise NotImplementedError

    def _filtered_sample(self, neg_sample):
        raise NotImplementedError

    def sample(self, pos_batch: torch.Tensor, replace: str = "both"):
        assert replace in ["head", "tail", "both"], f"replace should be in head, tail, both"

        neg_samples = self._sample(pos_batch, self.as_matrix, replace)

        if self.filter:
            neg_samples = self._filtered_sample(neg_samples)

        labels = self._label(pos_batch, self.as_matrix, replace)

        return neg_samples, labels


@NegativeSampler.register(name='no_sampling')
class NonNegativeSampler(NegativeSampler):
    def __init__(self, config: Config, dataset: DatasetProcessor, as_matrix: bool):
        super().__init__(config, dataset, as_matrix)

    def _sample(self, pos_batch, as_matrix, replace):
        batch_size = pos_batch.size(0)
        sample_size = pos_batch.size(1)
        vocab_size = self.dataset.num_entities()

        samples = pos_batch.repeat((1, vocab_size)).view(-1, sample_size)

        if replace == "tail":
            samples[:, 2] = torch.arange(vocab_size).repeat(batch_size)
        elif replace == "head":
            samples[:, 0] = torch.arange(vocab_size).repeat(batch_size)
        else:
            raise NotImplementedError

        if as_matrix:
            samples = samples.view(batch_size, -1)

        return samples

    def _label(self, pos_batch, as_matrix, replace):
        batch_size = pos_batch.size(0)
        vocab_size = self.dataset.num_entities()

        labels = torch.zeros((batch_size, vocab_size))

        if replace == "tail":
            labels[range(batch_size), pos_batch[:, 2].long()] = 1.
        elif replace == "head":
            labels[range(batch_size), pos_batch[:, 0].long()] = 1.
        else:
            raise NotImplementedError

        if not as_matrix:
            labels = labels.view(-1)

        return labels


@NegativeSampler.register(name='time_agnostic')
class BasicNegativeSampler(NegativeSampler):
    def __init__(self, config: Config, dataset: DatasetProcessor, as_matrix: bool):
        super().__init__(config, dataset, as_matrix)

    def _sample(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        # TODO 可不可以用generator 参考torch.RandomSampler
        """
        pos_batch should be batch_size * [head, rel, tail, time_info(...)]
        return tensor should be batch_size * (1+num_samples) * [head, rel, tail, time_info]
        """

        batch_size, dim = list(pos_batch.size())

        num_pos_neg = 1 + self.num_samples
        # TODO
        # if not hasattr(torch, repeat_interleave)
        pos_neg_samples_h = pos_batch.repeat((1, num_pos_neg)).view(-1, dim)
        pos_neg_samples_t = pos_neg_samples_h.clone()

        rand_nums_h = torch.randint(low=0, high=self.dataset.num_entities() - 1, size=(pos_neg_samples_h.shape[0], 1))
        rand_nums_t = torch.randint(low=0, high=self.dataset.num_entities() - 1, size=(pos_neg_samples_t.shape[0], 1))

        for i in range(pos_neg_samples_h.shape[0] // num_pos_neg):
            rand_nums_h[i * num_pos_neg] = 0
            rand_nums_t[i * num_pos_neg] = 0

        pos_neg_samples_h[:, 0] = (pos_neg_samples_h[:, 0] + rand_nums_h.squeeze()) % self.dataset.num_entities()
        pos_neg_samples_t[:, 2] = (pos_neg_samples_t[:, 2] + rand_nums_t.squeeze()) % self.dataset.num_entities()

        samples = torch.cat((pos_neg_samples_h, pos_neg_samples_t), dim=0)

        if as_matrix:
            samples = samples.view(2 * batch_size, -1)

        return samples

    def _label(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        batch_size = pos_batch.size(0)

        labels = torch.cat((torch.ones(batch_size, 1), torch.zeros(batch_size, self.num_samples)),
                           dim=1).repeat((2, 1))

        if not as_matrix:
            labels = labels.view(-1)

        return labels


@NegativeSampler.register(name="atise_time")
class AtiseTimeNegativeSampler(NegativeSampler):
    def __init__(self, config: Config, dataset: DatasetProcessor, as_matrix: bool):
        super().__init__(config, dataset, as_matrix)

    def _sample(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        batch_size, dim = list(pos_batch.size())

        samples = pos_batch.clone()

        for i in range(self.num_samples):
            samples = torch.cat((samples, pos_batch), 0)

        samples[:, 3] = torch.randint(self.dataset.num_timestamps(), (batch_size * self.num_samples))

        if as_matrix:
            raise NotImplementedError

        return torch.cat((pos_batch, samples), 0)

    def _label(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        batch_size, dim = list(pos_batch.size())

        ones = torch.ones((batch_size, 1))
        zeros = torch.zeros((batch_size * self.num_samples, 1))

        return torch.ones((ones, zeros), 0)


@NegativeSampler.register(name="self_adversarial")
class SelfAdversarialNegativeSampler(NegativeSampler):
    def __init__(self, config: Config, dataset: DatasetProcessor, as_matrix: bool):
        super().__init__(config, dataset, as_matrix)

    def _sample(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        raise NotImplementedError

    def _label(self, pos_batch: torch.Tensor, as_matrix: bool, replace: str):
        raise NotImplementedError


class DepNegativeSampler(Registrable):
    def __init__(self, config: Config, configuration_key: str, dataset: DatasetProcessor):
        super().__init__(config, configuration_key)

        # load config
        self.num_samples = torch.zeros(4, dtype=torch.int)
        self.filter_positives = torch.zeros(4, dtype=torch.bool)
        self.vocabulary_size = torch.zeros(4, dtype=torch.int)
        self.shared = self.get_option("shared")
        self.shared_type = self.check_option("shared_type", ["naive", "default"])
        self.with_replacement = self.get_option("with_replacement")
        if not self.with_replacement and not self.shared:
            raise ValueError(
                "Without replacement sampling is only supported when "
                "shared negative sampling is enabled."
            )
        self.filtering_split = config.get("negative_sampling.filtering.split")
        if self.filtering_split == "":
            self.filtering_split = config.get("train.split")
        for slot in SLOTS:
            slot_str = SLOT_STR[slot]
            self.num_samples[slot] = self.get_option(f"num_samples.{slot_str}")
            self.filter_positives[slot] = self.get_option(f"filtering.{slot_str}")
            self.vocabulary_size[slot] = (
                dataset.num_relations() if slot == P else dataset.num_timestamps() if slot == T else dataset.num_entities()
                # TODO edit tkge data
            )
            # create indices for filtering here already if needed and not existing
            # otherwise every worker would create every index again and again
            if self.filter_positives[slot]:
                pair = ["po", "so", "sp"][slot]
                dataset.index(f"{self.filtering_split}_{pair}_to_{slot_str}")

        if any(self.filter_positives):
            if self.shared:
                raise ValueError(
                    "Filtering is not supported when shared negative sampling is enabled."
                )

            self.filter_implementation = self.check_option(
                "filtering.implementation", ["standard", "fast", "fast_if_available"]
            )

            self.filter_implementation = self.get_option("filtering.implementation")

        self.dataset = dataset

        # auto config
        for slot, copy_from in [(S, O), (P, None), (O, S)]:
            if self.num_samples[slot] < 0:
                if copy_from is not None and self.num_samples[copy_from] > 0:
                    self.num_samples[slot] = self.num_samples[copy_from]
                else:
                    self.num_samples[slot] = 0

    @staticmethod
    def create(config: Config, configuration_key: str, dataset: DatasetProcessor):
        """Factory method for sampler creation"""

        sampling_type = config.get(configuration_key + ".sampling_type")

        if sampling_type in NegativeSampler.list_available():
            return NegativeSampler.by_name(sampling_type)(config, configuration_key, dataset)

        else:
            raise ValueError(
                f"{sampling_type} specified in configuration file is not supported"
                f"implement your negative sampler class with `NegativeSampler.register(name)"
            )

    def sample(self, positive_triples: torch.Tensor, slot: int, num_samples: Optional[int] = None):
        num_samples = self.num_samples[slot] if not num_samples else num_samples

        if self.shared:
            negative_samples = self._sample_shared(positive_triples, slot, num_samples).expand(positive_triples.size(0),
                                                                                               num_samples)
        else:
            negative_samples = self._sample(positive_triples, slot, num_samples)

        if self.filter_positives[slot]:
            if self.filter_implementation == "fast":
                negative_samples = self._filter_and_resample_fast(negative_samples, slot, positive_triples)
            elif self.filter_implementation == "standard":
                negative_samples = self._filter_and_resample(negative_samples, slot, positive_triples)
            else:
                try:
                    negative_samples = self._filter_and_resample_fast(negative_samples, slot, positive_triples)
                    self.filter_positives = "fast"
                except NotImplementedError:
                    negative_samples = self._filter_and_resample(negative_samples, slot, positive_triples)
                    self.filter_positives = "standard"

        return negative_samples

    def _sample(self, positive_triples: torch.Tensor, slot: int, num_samples: int):
        raise NotImplementedError

    def _sample_shared(self, positive_triples: torch.Tensor, slot: int, num_samples: int):
        raise NotImplementedError

    def _filter_and_resample_fast(self, negative_samples: torch.Tensor, slot: int, positive_triples: torch.Tensor):
        raise NotImplementedError

    def _filter_and_resample(self, negative_samples: torch.Tensor, slot: int, positive_triples: torch.Tensor):
        """Filter and resample indices until only negatives have been created. """
        pair_str = ["po", "so", "sp"][slot]
        # holding the positive indices for the respective pair
        index = self.dataset.index(f"train_{pair_str}_to_{SLOT_STR[slot]}")
        cols = [[P, O], [S, O], [S, P]][slot]
        pairs = positive_triples[:, cols]
        for i in range(positive_triples.size(0)):
            positives = index.get((pairs[i][0].item(), pairs[i][1].item())).numpy()
            # indices of samples that have to be sampled again

            resample_idx = where_in(negative_samples[i].numpy(), positives)
            # number of new samples needed
            num_new = len(resample_idx)
            # number already found of the new samples needed
            num_found = 0
            num_remaining = num_new - num_found
            while num_remaining:
                new_samples = self._sample(
                    positive_triples[i, None], slot, num_remaining
                ).view(-1)
                # indices of the true negatives
                tn_idx = where_in(new_samples.numpy(), positives, not_in=True)
                # write the true negatives found
                if len(tn_idx):
                    negative_samples[
                        i, resample_idx[num_found: num_found + len(tn_idx)]
                    ] = new_samples[tn_idx]
                    num_found += len(tn_idx)
                    num_remaining = num_new - num_found
        return negative_samples


@DepNegativeSampler.register(name="basic_sampler")
class BasicNegativeSampler(DepNegativeSampler):
    def _sample(self):
        super().__init__()

        raise NotImplementedError

    def sample(self):
        raise NotImplementedError


@DepNegativeSampler.register(name="uniform_sampler")
class UniformNegativeSampler(DepNegativeSampler):
    def __init__(self, config: Config, configuration_key: str, dataset: DatasetProcessor):
        super().__init__(config, configuration_key, dataset)

    def _sample(self, positive_triples: torch.Tensor, slot: int, num_samples: int):
        return torch.randint(
            self.vocabulary_size[slot], (positive_triples.size(0), num_samples)
        )

    def _sample_shared(self, positive_triples: torch.Tensor, slot: int, num_samples: int):
        return self._sample(torch.empty(1), slot, num_samples).view(-1)

    def _filter_and_resample_fast(self, negative_samples: torch.Tensor, slot: int, positive_triples: torch.Tensor):
        pair_str = ["po", "so", "sp"][slot]
        # holding the positive indices for the respective pair
        index = self.dataset.index(f"train_{pair_str}_to_{SLOT_STR[slot]}")
        cols = [[P, O], [S, O], [S, P]][slot]
        pairs = positive_triples[:, cols].numpy()
        batch_size = positive_triples.size(0)
        voc_size = self.vocabulary_size[slot]
        # filling a numba-dict here and then call the function was faster than 1. Using
        # numba lists 2. Using a python list and convert it to an np.array and use
        # offsets 3. Growing a np.array with np.append 4. leaving the loop in python and
        # calling a numba function within the loop
        positives_index = numba.typed.Dict()
        for i in range(batch_size):
            pair = (pairs[i][0], pairs[i][1])
        positives_index[pair] = index.get(pair).numpy()
        negative_samples = negative_samples.numpy()
        UniformNegativeSampler._filter_and_resample_numba(
            negative_samples, pairs, positives_index, batch_size, int(voc_size),
        )
        return torch.tensor(negative_samples, dtype=torch.int64)

    @numba.njit
    def _filter_and_resample_numba(negative_samples, pairs, positives_index, batch_size, voc_size):
        for i in range(batch_size):
            positives = positives_index[(pairs[i][0], pairs[i][1])]
            # inlining the where_in function here results in an internal numba
            # error which asks to file a bug report
            resample_idx = where_in(negative_samples[i], positives)
            # number of new samples needed
            num_new = len(resample_idx)
            # number already found of the new samples needed
            num_found = 0
            num_remaining = num_new - num_found
            while num_remaining:
                new_samples = np.random.randint(0, voc_size, num_remaining)
                idx = where_in(new_samples, positives, not_in=True)
                # write the true negatives found
                if len(idx):
                    ctr = 0
                    # numba does not support advanced indexing but the loop
                    # is optimized so it's faster than numpy anyway
                    for j in resample_idx[num_found: num_found + len(idx)]:
                        negative_samples[i, j] = new_samples[ctr]
                        ctr += 1
                    num_found += len(idx)
                    num_remaining = num_new - num_found

# @NegativeSampler.register(name="time_agonostic_sampler")
# class TimeAgnosticNegativeSampler(NegativeSampler):
#     def _sample(self):
#         raise NotImplementedError
#
#
# @NegativeSampler.register(name="time_dependent_sampler")
# class TimeDependentNegativeSampler(NegativeSampler):
#     def _sample(self):
#         raise NotImplementedError
