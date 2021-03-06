# coding=utf-8
# Copyright 2019 The Meta-Dataset Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module assembles full input data pipelines.

The whole pipeline incorporate (potentially) multiple Readers, the logic to
select between them, and the common logic to extract support / query sets if
needed, decode the example strings, and resize the images.
"""
# TODO(lamblinp): Organize the make_*_pipeline functions into classes, and
# make them output Batch or EpisodeDataset objects directly.
# TODO(lamblinp): Update variable names to be more consistent
# - target, class_idx, label

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

import gin.tf
from meta_dataset import data
from meta_dataset.data import learning_spec
from meta_dataset.data import reader
from meta_dataset.data import sampling
import tensorflow as tf


def filter_dummy_examples(example_strings, class_ids):
  """Returns tensors with only actual examples, filtering out the dummy ones.

  Actual examples are the first ones in the tensors, and followed by dummy ones,
  indicated by negative class IDs.

  Args:
    example_strings: 1-D Tensor of dtype str, Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset, except for negative ones, that indicate dummy examples).
  """
  num_actual = tf.reduce_sum(tf.cast(class_ids >= 0, tf.int32))
  actual_example_strings = example_strings[:num_actual]
  actual_class_ids = class_ids[:num_actual]
  return (actual_example_strings, actual_class_ids)


def _log_data_augmentation(data_augmentation, name):
  """Logs the given data augmentation parameters for diagnostic purposes."""
  if not data_augmentation:
    tf.logging.info('No data augmentation provided for %s', name)
  else:
    tf.logging.info('%s augmentations:', name)
    tf.logging.info('enable_jitter: %s', data_augmentation.enable_jitter)
    tf.logging.info('jitter_amount: %d', data_augmentation.jitter_amount)
    tf.logging.info('enable_gaussian_noise: %s',
                    data_augmentation.enable_gaussian_noise)
    tf.logging.info('gaussian_noise_std: %s',
                    data_augmentation.gaussian_noise_std)


@gin.configurable(
    whitelist=['support_data_augmentation', 'query_data_augmentation'])
def process_episode(example_strings,
                    class_ids,
                    chunk_sizes,
                    image_size,
                    support_data_augmentation=None,
                    query_data_augmentation=None):
  """Processes an episode.

  This function:

  1) splits the batch of examples into "flush", "support", and "query" chunks,
  2) throws away the "flush" chunk,
  3) removes the padded dummy examples from the "support" and "query" chunks,
     and
  4) extracts and processes images out of the example strings.
  5) builds support and query targets (numbers from 0 to K-1 where K is the
     number of classes in the episode) from the class IDs.

  Args:
    example_strings: 1-D Tensor of dtype str, tf.train.Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset).
    chunk_sizes: Tuple of 3 ints representing the sizes of (resp.) the flush,
      support, and query chunks.
    image_size: int, desired image size used during decoding.
    support_data_augmentation: A DataAugmentation object with parameters for
      perturbing the support set images.
    query_data_augmentation: A DataAugmentation object with parameters for
      perturbing the query set images.

  Returns:
    support_images, support_labels, support_class_ids, query_images,
      query_labels, query_class_ids: Tensors, batches of images, labels, and
      (absolute) class IDs, for the support and query sets (respectively).
  """
  _log_data_augmentation(support_data_augmentation, 'support')
  _log_data_augmentation(query_data_augmentation, 'query')
  flush_chunk_size, support_chunk_size, _ = chunk_sizes
  support_start = flush_chunk_size
  query_start = support_start + support_chunk_size
  support_map_fn = functools.partial(
      process_example,
      image_size=image_size,
      data_augmentation=support_data_augmentation)
  query_map_fn = functools.partial(
      process_example,
      image_size=image_size,
      data_augmentation=query_data_augmentation)

  support_strings = example_strings[support_start:query_start]
  support_class_ids = class_ids[support_start:query_start]
  (support_strings, support_class_ids) = filter_dummy_examples(
      support_strings, support_class_ids)
  support_images = tf.map_fn(
      support_map_fn, support_strings, dtype=tf.float32, back_prop=False)

  query_strings = example_strings[query_start:]
  query_class_ids = class_ids[query_start:]
  (query_strings, query_class_ids) = filter_dummy_examples(
      query_strings, query_class_ids)
  query_images = tf.map_fn(
      query_map_fn, query_strings, dtype=tf.float32, back_prop=False)

  # Convert class IDs into labels in [0, num_ways).
  _, support_labels = tf.unique(support_class_ids)
  _, query_labels = tf.unique(query_class_ids)

  return (support_images, support_labels, support_class_ids, query_images,
          query_labels, query_class_ids)


@gin.configurable(whitelist=['batch_data_augmentation'])
def process_batch(example_strings,
                  class_ids,
                  image_size,
                  batch_data_augmentation=None):
  """Processes a batch.

  This function:

  1) extracts and processes images out of the example strings.
  2) builds targets from the class ID and offset.

  Args:
    example_strings: 1-D Tensor of dtype str, Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset).
    image_size: int, desired image size used during decoding.
    batch_data_augmentation: A DataAugmentation object with parameters for
      perturbing the batch images.

  Returns:
    images, labels: Tensors, a batch of image and labels.
  """
  _log_data_augmentation(batch_data_augmentation, 'batch')
  map_fn = functools.partial(
      process_example,
      image_size=image_size,
      data_augmentation=batch_data_augmentation)
  images = tf.map_fn(map_fn, example_strings, dtype=tf.float32, back_prop=False)
  labels = class_ids
  return (images, labels)


def process_example(example_string, image_size, data_augmentation=None):
  """Processes a single example string.

  Extracts and processes the image, and ignores the label. We assume that the
  image has three channels.

  Args:
    example_string: str, an Example protocol buffer.
    image_size: int, desired image size. The extracted image will be resized to
      `[image_size, image_size]`.
    data_augmentation: A DataAugmentation object with parameters for perturbing
      the images.

  Returns:
    image_rescaled: the image, resized to `image_size x image_size` and rescaled
      to [-1, 1]. Note that Gaussian data augmentation may cause values to
      go beyond this range.
  """
  image_string = tf.parse_single_example(
      example_string,
      features={
          'image': tf.FixedLenFeature([], dtype=tf.string),
          'label': tf.FixedLenFeature([], tf.int64)
      })['image']
  image_decoded = tf.image.decode_jpeg(image_string, channels=3)
  image_resized = tf.image.resize_images(
      image_decoded, [image_size, image_size],
      method=tf.image.ResizeMethod.BILINEAR,
      align_corners=True)
  image = 2 * (image_resized / 255.0 - 0.5)  # Rescale to [-1, 1].

  if data_augmentation is not None:
    if data_augmentation.enable_gaussian_noise:
      image = image + tf.random_normal(
          tf.shape(image)) * data_augmentation.gaussian_noise_std

    if data_augmentation.enable_jitter:
      j = data_augmentation.jitter_amount
      paddings = tf.constant([[j, j], [j, j], [0, 0]])
      image = tf.pad(image, paddings, 'REFLECT')
      image = tf.image.random_crop(image, [image_size, image_size, 3])

  return image


def make_one_source_episode_pipeline(dataset_spec,
                                     use_dag_ontology,
                                     use_bilevel_ontology,
                                     split,
                                     pool=None,
                                     num_ways=None,
                                     num_support=None,
                                     num_query=None,
                                     shuffle_buffer_size=None,
                                     read_buffer_size_bytes=None,
                                     image_size=None):
  """Returns a pipeline emitting data from one single source as Episodes.

  Args:
    dataset_spec: A DatasetSpecification object defining what to read from.
    use_dag_ontology: Whether to use source's ontology in the form of a DAG to
      sample episodes classes.
    use_bilevel_ontology: Whether to use source's bilevel ontology (consisting
      of superclasses and subclasses) to sample episode classes.
    split: A learning_spec.Split object identifying the source (meta-)split.
    pool: String (optional), for example-split datasets, which example split to
      use ('train', 'valid', or 'test'), used at meta-test time only.
    num_ways: Integer (optional), fixes the number of classes ("ways") to be
      used in each episode if provided.
    num_support: Integer (optional), fixes the number of examples for each class
      in the support set if provided.
    num_query: Integer (optional), fixes the number of examples for each class
      in the query set if provided.
    shuffle_buffer_size: int or None, shuffle buffer size for each Dataset.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    image_size: int, desired image size used during decoding.

  Returns:
    A Dataset instance that outputs fully-assembled and decoded episodes.
  """
  if pool is not None:
    if not data.POOL_SUPPORTED:
      raise NotImplementedError('Example-level splits or pools not supported.')
  else:
    use_all_classes = False
  episode_reader = reader.EpisodeReader(
      dataset_spec, split, shuffle_buffer_size, read_buffer_size_bytes)
  sampler = sampling.EpisodeDescriptionSampler(
      episode_reader.dataset_spec,
      split,
      pool=pool,
      use_dag_hierarchy=use_dag_ontology,
      use_bilevel_hierarchy=use_bilevel_ontology,
      use_all_classes=use_all_classes,
      num_ways=num_ways,
      num_support=num_support,
      num_query=num_query)
  dataset = episode_reader.create_dataset_input_pipeline(sampler, pool=pool)

  # Episodes coming out of `dataset` contain flushed examples and are internally
  # padded with dummy examples. `process_episode` discards flushed examples,
  # splits the episode into support and query sets, removes the dummy examples
  # and decodes the example strings.
  chunk_sizes = sampler.compute_chunk_sizes()
  map_fn = functools.partial(
      process_episode, chunk_sizes=chunk_sizes, image_size=image_size)
  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


def make_multisource_episode_pipeline(dataset_spec_list,
                                      use_dag_ontology_list,
                                      use_bilevel_ontology_list,
                                      split,
                                      pool=None,
                                      num_ways=None,
                                      num_support=None,
                                      num_query=None,
                                      shuffle_buffer_size=None,
                                      read_buffer_size_bytes=None,
                                      image_size=None):
  """Returns a pipeline emitting data from multiple sources as Episodes.

  Each episode only contains data from one single source. For each episode, its
  source is sampled uniformly across all sources.

  Args:
    dataset_spec_list: A list of DatasetSpecification, one for each source.
    use_dag_ontology_list: A list of Booleans, one for each source: whether to
      use that source's DAG-structured ontology to sample episode classes.
    use_bilevel_ontology_list: A list of Booleans, one for each source: whether
      to use that source's bi-level ontology to sample episode classes.
    split: A learning_spec.Split object identifying the sources split. It is the
      same for all datasets.
    pool: String (optional), for example-split datasets, which example split to
      use ('train', 'valid', or 'test'), used at meta-test time only.
    num_ways: Integer (optional), fixes the number of classes ("ways") to be
      used in each episode if provided.
    num_support: Integer (optional), fixes the number of examples for each class
      in the support set if provided.
    num_query: Integer (optional), fixes the number of examples for each class
      in the query set if provided.
    shuffle_buffer_size: int or None, shuffle buffer size for each Dataset.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    image_size: int, desired image size used during decoding.

  Returns:
    A Dataset instance that outputs fully-assembled and decoded episodes.
  """
  if pool is not None:
    if not data.POOL_SUPPORTED:
      raise NotImplementedError('Example-level splits or pools not supported.')
  sources = []
  for (dataset_spec, use_dag_ontology, use_bilevel_ontology) in zip(
      dataset_spec_list, use_dag_ontology_list, use_bilevel_ontology_list):
    episode_reader = reader.EpisodeReader(
        dataset_spec, split, shuffle_buffer_size, read_buffer_size_bytes)
    sampler = sampling.EpisodeDescriptionSampler(
        episode_reader.dataset_spec,
        split,
        pool=pool,
        use_dag_hierarchy=use_dag_ontology,
        use_bilevel_hierarchy=use_bilevel_ontology,
        num_ways=num_ways,
        num_support=num_support,
        num_query=num_query)
    dataset = episode_reader.create_dataset_input_pipeline(sampler, pool=pool)
    sources.append(dataset)

  # Sample uniformly among sources
  dataset = tf.data.experimental.sample_from_datasets(sources)

  # Episodes coming out of `dataset` contain flushed examples and are internally
  # padded with dummy examples. `process_episode` discards flushed examples,
  # splits the episode into support and query sets, removes the dummy examples
  # and decodes the example strings.
  chunk_sizes = sampler.compute_chunk_sizes()
  map_fn = functools.partial(
      process_episode, chunk_sizes=chunk_sizes, image_size=image_size)
  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


def make_one_source_batch_pipeline(dataset_spec,
                                   split,
                                   batch_size,
                                   pool=None,
                                   shuffle_buffer_size=None,
                                   read_buffer_size_bytes=None,
                                   image_size=None):
  """Returns a pipeline emitting data from one single source as Batches.

  Args:
    dataset_spec: A DatasetSpecification object defining what to read from.
    split: A learning_spec.Split object identifying the source split.
    batch_size: An int representing the max number of examples in each batch.
    pool: String (optional), for example-split datasets, which example split to
      use ('valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, number of examples in the buffer used for
      shuffling the examples from different classes, while they are mixed
      together. There is only one shuffling operation, not one per class.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    image_size: int, desired image size used during decoding.

  Returns:
    A Dataset instance that outputs decoded batches from all classes in the
    split.
  """
  batch_reader = reader.BatchReader(dataset_spec, split, shuffle_buffer_size,
                                    read_buffer_size_bytes)
  dataset = batch_reader.create_dataset_input_pipeline(
      batch_size=batch_size, pool=pool)
  map_fn = functools.partial(process_batch, image_size=image_size)
  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


# TODO(lamblinp): Update this option's name
@gin.configurable('BatchSplitReaderGetReader', whitelist=['add_dataset_offset'])
def make_multisource_batch_pipeline(dataset_spec_list,
                                    split,
                                    batch_size,
                                    add_dataset_offset,
                                    pool=None,
                                    shuffle_buffer_size=None,
                                    read_buffer_size_bytes=None,
                                    image_size=None):
  """Returns a pipeline emitting data from multiple source as Batches.

  Args:
    dataset_spec_list: A list of DatasetSpecification, one for each source.
    split: A learning_spec.Split object identifying the source split.
    batch_size: An int representing the max number of examples in each batch.
    add_dataset_offset: A Boolean, whether to add an offset to each dataset's
      targets, so that each target is unique across all datasets.
    pool: String (optional), for example-split datasets, which example split to
      use ('valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, number of examples in the buffer used for
      shuffling the examples from different classes, while they are mixed
      together. There is only one shuffling operation, not one per class.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    image_size: int, desired image size used during decoding.

  Returns:
    A Dataset instance that outputs decoded batches from all classes in the
    split.
  """
  sources = []
  offset = 0
  for dataset_spec in dataset_spec_list:
    batch_reader = reader.BatchReader(dataset_spec, split, shuffle_buffer_size,
                                      read_buffer_size_bytes)
    dataset = batch_reader.create_dataset_input_pipeline(
        batch_size=batch_size, pool=pool, offset=offset)
    sources.append(dataset)
    if add_dataset_offset:
      offset += len(dataset_spec.get_classes(split))

  # Sample uniformly among sources
  dataset = tf.data.experimental.sample_from_datasets(sources)

  map_fn = functools.partial(process_batch, image_size=image_size)
  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset
