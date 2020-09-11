# Copyright 2020 The CLU Authors.
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

r"""Helper functions for building deterministic tf.data input pipelines.

The function `create_dataset()` makes it easy to build a `tf.data` based input
pipeline that allows for completely reproducible results based on a single
initial random seed. The caller must take care to create a unique initial seed
on every host that is then passed to `create_dataset()`, where further unique
random keys are derived for every batch. Within a single batch, this key is
exposed as the special feature "rng" and can be used to implement stateless
preprocessing functions.

The function `get_read_instruction_for_host()` makes it easy to split a dataset
evenly between multiple hosts in a SPMD setup with multiple machines. Within a
single host, every batch is usually distributed to all the attached accelerators
(the first value of the `batch_dims` argument to `create_dataset()`).

The function `create_distributed_dataset()` finally is intended to be used in
conjunction with a `tf.distribute.Strategy`.

Synopsis for deterministic training with multiple hosts:

  import jax
  from clu import deterministic_data

  rng = jax.random.PRNGKey(42)  # Global RNG (e.g. from config)
  rng = jax.random.fold_in(rng, jax.host_id())  # Derive RNG for this host.
  dataset_builder = tfds.builder(...)
  split = deterministic_data.get_read_instruction_for_host(
      "train", dataset_builder.info.splits["train"].num_examples)
  ds = deterministic_data.create_dataset(
      dataset_builder,
      split=split,
      rng=rng
  )
  ds_iter = iter(ds)
  for _ in range(num_train_steps):
    batch = jax.tree_map(lambda x: x._numpy(), next(ds_iter)
    # (training step)

"""

from typing import Callable, Dict, Optional, Sequence, Union

from absl import logging
from clu.google import usage_logging
import jax
import jax.numpy as jnp
import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds

usage_logging.log_import("deterministic_data")

Tensor = Union[tf.Tensor, tf.SparseTensor, tf.RaggedTensor]
Features = Dict[str, Tensor]

AUTOTUNE = tf.data.experimental.AUTOTUNE


def get_read_instruction_for_host(
    split: str,
    num_examples: int,
    *,
    host_id: Optional[int] = None,
    host_count: Optional[int] = None,
    drop_remainder: bool = True) -> tfds.core.ReadInstruction:
  """Returns a ReadInstruction of the data range for this host.

  In a distributed setting all hosts should get the same number of examples.
  This can exclude a few (< host_count) examples.

  The examples are distributed evenly across the hosts, and remaining examples
  are distributed to the hosts with the lowest id.

  Assuming a single epoch, the number of batches (e.g. for
  `create_dataset(pad_up_to_batches)`) can be computed by:

    batches = int(np.ceil(num_examples / global_batch_size))

  Args:
    split: Name of the dataset split to use.
    num_examples: Number of examples of the split.
    host_id: Optional, host index in [0, host_count). Defaults to
      `jax.host_id()`.
    host_count: Optional, number of hosts. Defaults to `jax.host_count`.
    drop_remainder: If True drop the remaining examples (at the end of the
      dataset) that cannot be equally distributed across hosts. If False the
      remaining examples will be distributed across the hosts.

  Returns:
    A tfds.core.ReadInstruction specifying the range of examples to use on this
    host.
  """
  if host_id is None:
    host_id = jax.host_id()
  if host_count is None:
    host_count = jax.host_count()
  if host_id < 0 or host_id >= host_count or host_count < 1:
    raise ValueError(
        f"Invalid combination of host_id ({host_id}) and host_count "
        f"({host_count}).")

  examples_per_host = num_examples // host_count
  start = examples_per_host * host_id
  end = examples_per_host * (host_id + 1)

  # Handle remaining examples.
  num_unused_examples = num_examples - examples_per_host * host_count
  assert num_unused_examples >= 0, num_unused_examples
  assert num_unused_examples < host_count, num_unused_examples
  if num_unused_examples > 0:
    if drop_remainder:
      logging.warning("Dropping %d examples of %d examples (host count: %d).",
                      num_unused_examples, num_examples, host_count)
    else:
      # The first `num_unused_examples` hosts get one extra example.
      start += min(host_id, num_unused_examples)
      end += min(host_id + 1, num_unused_examples)

  return tfds.core.ReadInstruction(split, from_=start, to=end, unit="abs")


def _preprocess_with_per_example_rng(ds: tf.data.Dataset,
                                     preprocess_fn: Callable[[Features],
                                                             Features], *,
                                     rng: jnp.ndarray) -> tf.data.Dataset:
  """Maps `ds` using the preprocess_fn and a deterministic RNG per example.

  Args:
    ds: Dataset containing Python dictionary with the features. The 'rng'
      feature should not exist.
    preprocess_fn: Preprocessing function that takes a Python dictionary of
      tensors and returns a Python dictionary of tensors. The function should be
      convertible into a TF graph.
    rng: Base RNG to use. Per example RNGs will be derived from this by folding
      in the example index.

  Returns:
    The dataset mapped by the `preprocess_fn`.
  """

  def _fn(example_index: int, features: Features) -> Features:
    example_index = tf.cast(example_index, tf.int32)
    features["rng"] = tf.random.experimental.stateless_fold_in(
        tf.cast(rng, tf.int64), example_index)
    processed = preprocess_fn(features)
    if isinstance(processed, dict) and "rng" in processed:
      del processed["rng"]
    return processed

  return ds.enumerate().map(_fn, num_parallel_calls=AUTOTUNE)


def create_dataset(dataset_builder,
                   *,
                   split: Union[str, tfds.core.ReadInstruction],
                   batch_dims: Sequence[int] = (),
                   rng: Optional[jnp.ndarray] = None,
                   filter_fn: Optional[Callable[[Features], bool]] = None,
                   preprocess_fn: Optional[Callable[[Features],
                                                    Features]] = None,
                   decoders: Optional[Dict[str, tfds.decode.Decoder]] = None,
                   cache: bool = False,
                   num_epochs: Optional[int] = None,
                   shuffle: bool = True,
                   shuffle_buffer_size: int = 10_000,
                   prefetch_size: int = 4,
                   pad_up_to_batches: Optional[int] = None) -> tf.data.Dataset:
  """Create standard input pipeline (shuffle, preprocess, batch).

  Args:
    dataset_builder: Dataset builder object with a as_dataset() method. E.g.
      instance of `tfds.core.DatasetBuilder` as returned by `tfds.builder(...)`.
    split: Specifies which split of the data to load. Passed on to
      `tfds.DatasetBuilder.as_dataset()`. See also the
      [split API guide](https://www.tensorflow.org/datasets/splits). In a multi
        host setup, this parameter can conveniently be generated by the function
        `get_read_instruction_for_host()`.
    batch_dims: List of size of batch dimensions. Multiple batch dimension can
      be used to provide inputs for multiple devices. E.g.
      [jax.local_device_count(), batch_size // jax.device_count()].
    rng: A jax.random.PRNG key to use of seeding shuffle operations and
      preprocessing ops. Must be set if shuffling.
    filter_fn: Optional function to filter the decoded examples. This happens
      before the preprocessing.
    preprocess_fn: Function for preprocessing individual examples (which should
      be Python dictionary of tensors)
    decoders: Optional dictionary of decoder passed to as_dataset.
    cache: Cache the unprocessed dataset in memory.
    num_epochs: Number of epochs for which to repeat the dataset. None to repeat
      forever.
    shuffle: Whether the shuffle the dataset (both on the file and example
      level).
    shuffle_buffer_size: Number of examples in the shuffle buffer.
    prefetch_size: The number of elements in the final dataset to prefetch in
      the background. This should be a small (say <10) positive integer or
      tf.data.experimental.AUTOTUNE.
    pad_up_to_batches: Set this option to process the entire dataset. When set,
      then the dataset is first padded to the specified number of batches. A new
      feature called "mask" is added to every batch. This feature is set to
      `True` for every example that comes from `dataset_builder`, and to `False`
      for every example that is padded to get to the specified number of
      batches. Note that the specified `dataset_builder` and `split` must result
      in at least `pad_up_to_batches` (possibly partial) batches.

  Returns:
    The dataset with preprocessed and batched examples.
  """
  rng_available = rng is not None
  if not rng_available and shuffle:
    raise ValueError("Please set 'rng' when shuffling.")
  if rng_available:
    rngs = list(jax.random.split(rng, 3))
  else:
    rngs = 3 * [[None, None]]

  dataset_options = tf.data.Options()
  dataset_options.experimental_optimization.map_parallelization = True
  dataset_options.experimental_threading.private_threadpool_size = 48
  dataset_options.experimental_threading.max_intra_op_parallelism = 1

  read_config = tfds.ReadConfig(
      shuffle_seed=rngs.pop()[0], options=dataset_options)
  ds = dataset_builder.as_dataset(
      split=split,
      shuffle_files=shuffle,
      read_config=read_config,
      decoders=decoders)

  if filter_fn is not None:
    ds = ds.filter(filter_fn)

  if cache:
    ds = ds.cache()
  ds = ds.repeat(num_epochs)
  if shuffle:
    ds = ds.shuffle(shuffle_buffer_size, seed=rngs.pop()[0])

  if preprocess_fn is not None:
    if rng_available:
      ds = _preprocess_with_per_example_rng(ds, preprocess_fn, rng=rngs.pop())
    else:
      ds = ds.map(preprocess_fn, num_parallel_calls=AUTOTUNE)

  if pad_up_to_batches:
    filler_ds = tf.data.Dataset.from_tensor_slices(
        dict(
            **{
                name: tf.zeros(spec.shape, spec.dtype)[None]
                for name, spec in ds.element_spec.items()
            },
            mask=[False]))
    ds = ds.map(
        lambda features: dict(mask=True, **features),
        num_parallel_calls=AUTOTUNE)
    padding = pad_up_to_batches * np.prod(batch_dims) - int(ds.cardinality())
    assert padding >= 0, (
        f"Invalid padding={padding} (batch_dims={batch_dims}, cardinality="
        f"{ds.cardinality()}, pad_up_to_batches={pad_up_to_batches})")
    ds = ds.concatenate(filler_ds.repeat(padding))

  if batch_dims:
    for batch_size in reversed(batch_dims):
      ds = ds.batch(batch_size, drop_remainder=True)

  return ds.prefetch(prefetch_size)


def create_distributed_dataset(
    dataset_builder,
    *,
    split: Union[str, tfds.core.ReadInstruction],
    global_batch_size: int,
    strategy: tf.distribute.Strategy,
    rng: Optional[jnp.ndarray] = None,
    filter_fn: Optional[Callable[[Features], bool]] = None,
    preprocess_fn: Optional[Callable[[Features], Features]] = None,
    decoders: Optional[Dict[str, tfds.decode.Decoder]] = None,
    cache: bool = False,
    num_epochs: Optional[int] = None,
    shuffle: bool = True,
    shuffle_buffer_size: int = 10_000,
    prefetch_size: int = 4,
    pad_up_to_batches: Optional[int] = None) -> tf.data.Dataset:
  """Create standard input pipeline (shuffle, preprocess, batch).

  Args:
    dataset_builder: Dataset builder object with a as_dataset() method. E.g.
      instance of `tfds.core.DatasetBuilder` as returned by `tfds.builder(...)`.
    split: Split name to use, will be passed to as_dataset().
    global_batch_size: Global batch size for all input pipelines together.
    strategy: Distribution strategy for distributing the dataset.
    rng: A jax.random.PRNG key to use of seeding shuffle operations and
      preprocessing ops.
    filter_fn: Optional function to filter the decoded examples. This happens
      before the preprocessing.
    preprocess_fn: Function for preprocessing individual examples (which should
      be Python dictionary of tensors)
    decoders: Optional dictionary of decoder passed to as_dataset.
    cache: Cache the unprocessed dataset in memory.
    num_epochs: Number of epochs for which to repeat the dataset. None to repeat
      forever.
    shuffle: Whether the shuffle the dataset (both on the file and example
      level).
    shuffle_buffer_size: Number of examples in the shuffle buffer.
    prefetch_size: The number of elements in the final dataset to prefetch in
      the background. This should be a small (say <10) positive integer or
      tf.data.experimental.AUTOTUNE.
    pad_up_to_batches: Set this option to process the entire dataset. When set,
      then the dataset is first padded to the specified number of batches. A new
      feature called "mask" is added to every batch. This feature is set to
      `True` for every example that comes from `dataset_builder`, and to `False`
      for every example that is padded to get to the specified number of
      batches. Note that the specified `dataset_builder` and `split` must
      provide at least `pad_up_to_batches` (possibly partial) batches.

  Returns:
    The dataset with preprocessed and batched examples.
  """

  def dataset_fn(input_context: tf.distribute.InputContext):
    """Returns the dataset for a single worker."""
    logging.info("dataset_fn(input_context=%s)", input_context)

    if rng is None:
      local_rng = None
    else:
      local_rng = jax.random.fold_in(rng, input_context.input_pipeline_id)
    per_replica_batch_size = input_context.get_per_replica_batch_size(
        global_batch_size)
    return create_dataset(
        dataset_builder=dataset_builder,
        split=split,
        batch_dims=[per_replica_batch_size],
        rng=local_rng,
        filter_fn=filter_fn,
        preprocess_fn=preprocess_fn,
        decoders=decoders,
        cache=cache,
        num_epochs=num_epochs,
        shuffle=shuffle,
        shuffle_buffer_size=shuffle_buffer_size,
        prefetch_size=prefetch_size,
        pad_up_to_batches=pad_up_to_batches)

  return strategy.experimental_distribute_datasets_from_function(dataset_fn)


def skip_decoders(
    builder: tfds.core.DatasetBuilder) -> Dict[str, tfds.decode.Decoder]:
  """Skips decoding of features "image" and "video" (if present).

  The decoders returned by this.function can be used e.g. for the `decoders`
  argument in the function `create_dataset()`.

  Args:
    builder: A tensorflow_datasets builder.

  Returns:
    A dictionary mapping "image" and/or "video" features to the special decoder
    `tfds.decode.SkipDecoding()` that skips the decoding entirely.
  """
  encoded_features = set(builder.info.features) & {"image", "video"}
  return {k: tfds.decode.SkipDecoding() for k in encoded_features}
