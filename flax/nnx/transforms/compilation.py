# Copyright 2024 The Flax Authors.
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
# pytype: skip-file

import dataclasses
import functools
import typing as tp

from flax.nnx import (
  extract,
  filterlib,
  graph,
  statelib,
  variablelib,
)
import jax
import jax.core
import jax.stages

from flax.typing import Missing

F = tp.TypeVar('F', bound=tp.Callable[..., tp.Any])


# -------------------------------
# jit
# -------------------------------


class StateSharding(extract.PrefixMapping):
  def __init__(
    self,
    filter_sharding: statelib.State
    | tp.Mapping[filterlib.Filter, tp.Any]
    | tp.Iterable[tuple[filterlib.Filter, tp.Any]],
    /,
  ):
    if isinstance(filter_sharding, statelib.State):
      filter_sharding = statelib.create_path_filters(filter_sharding)  # type: ignore

    iterable = tuple(
      filter_sharding.items()
      if isinstance(filter_sharding, tp.Mapping)
      else filter_sharding
    )
    self._filters = tuple(filter for filter, _ in iterable)
    self._shardings = tuple(axis for _, axis in iterable)

  @property
  def filters(self) -> tuple[filterlib.Filter, ...]:
    return self._filters

  @property
  def shardings(self) -> tuple[tp.Any, ...]:
    return self._shardings

  def map_prefix(
    self, path: variablelib.PathParts, variable: variablelib.Variable
  ) -> tp.Any:
    for filter, sharding in zip(self.filters, self.shardings):
      predicate = filterlib.to_predicate(filter)
      if predicate(path, variable):
        return sharding
    raise ValueError(f'No axis found for {path=}, {variable=}')

  def __repr__(self):
    return f'StateSharding({dict(zip(self.filters, self.shardings))})'

  def __eq__(self, other):
    return (
      isinstance(other, StateSharding)
      and self.filters == other.filters
      and self.shardings == other.shardings
    )

  def __hash__(self):
    return hash((self.filters, self.shardings))


def _jit_split_fn(ctx: graph.SplitContext, path, prefix, x):
  if isinstance(prefix, StateSharding):
    return extract.NodeStates.from_split(
      *ctx.split(x, *prefix.filters), metadata=prefix
    )
  return extract.NodeStates.from_split(*ctx.split(x))


@dataclasses.dataclass(eq=False)
class JitFn:
  f: tp.Callable[..., tp.Any]
  in_shardings: tp.Any
  out_shardings: tp.Any
  kwarg_shardings: tp.Any

  def __post_init__(self):
    functools.update_wrapper(self, self.f)

  def __call__(self, *pure_args, **pure_kwargs):
    args, kwargs = extract.from_tree((pure_args, pure_kwargs), ctxtag='jit')

    out = self.f(*args, **kwargs)

    args_out, kwargs_out = extract.clear_non_graph_nodes((args, kwargs))
    pure_args_out, pure_kwargs_out, pure_out = extract.to_tree(
      (args_out, kwargs_out, out),
      prefix=(self.in_shardings, self.kwarg_shardings, self.out_shardings),
      ctxtag='jit',
      split_fn=_jit_split_fn,
    )

    return pure_args_out, pure_kwargs_out, pure_out


@tp.overload
def jit(
  *,
  in_shardings: tp.Any = None,
  out_shardings: tp.Any = None,
  static_argnums: int | tp.Sequence[int] | None = None,
  static_argnames: str | tp.Iterable[str] | None = None,
  donate_argnums: int | tp.Sequence[int] | None = None,
  donate_argnames: str | tp.Iterable[str] | None = None,
  keep_unused: bool = False,
  device: tp.Optional[jax.Device] = None,
  backend: tp.Optional[str] = None,
  inline: bool = False,
  abstracted_axes: tp.Optional[tp.Any] = None,
) -> tp.Callable[[F], F]: ...
@tp.overload
def jit(
  fun: F,
  *,
  in_shardings: tp.Any = None,
  out_shardings: tp.Any = None,
  static_argnums: int | tp.Sequence[int] | None = None,
  static_argnames: str | tp.Iterable[str] | None = None,
  donate_argnums: int | tp.Sequence[int] | None = None,
  donate_argnames: str | tp.Iterable[str] | None = None,
  keep_unused: bool = False,
  device: tp.Optional[jax.Device] = None,
  backend: tp.Optional[str] = None,
  inline: bool = False,
  abstracted_axes: tp.Optional[tp.Any] = None,
) -> F: ...
def jit(
  fun: F | type[Missing] = Missing,
  *,
  in_shardings: tp.Any = None,
  out_shardings: tp.Any = None,
  static_argnums: int | tp.Sequence[int] | None = None,
  static_argnames: str | tp.Iterable[str] | None = None,
  donate_argnums: int | tp.Sequence[int] | None = None,
  donate_argnames: str | tp.Iterable[str] | None = None,
  keep_unused: bool = False,
  device: tp.Optional[jax.Device] = None,
  backend: tp.Optional[str] = None,
  inline: bool = False,
  abstracted_axes: tp.Optional[tp.Any] = None,
) -> F | tp.Callable[[F], F]:
  """
  Lifted version of ``jax.jit`` that can handle Modules / graph nodes as
  arguments.

  Args:
    fun: Function to be jitted. ``fun`` should be a pure function, as
      side-effects may only be executed once.

      The arguments and return value of ``fun`` should be arrays,
      scalars, or (nested) standard Python containers (tuple/list/dict) thereof.
      Positional arguments indicated by ``static_argnums`` can be anything at
      all, provided they are hashable and have an equality operation defined.
      Static arguments are included as part of a compilation cache key, which is
      why hash and equality operators must be defined.

      JAX keeps a weak reference to ``fun`` for use as a compilation cache key,
      so the object ``fun`` must be weakly-referenceable. Most :class:`Callable`
      objects will already satisfy this requirement.
    in_shardings: Pytree of structure matching that of arguments to ``fun``,
      with all actual arguments replaced by resource assignment specifications.
      It is also valid to specify a pytree prefix (e.g. one value in place of a
      whole subtree), in which case the leaves get broadcast to all values in
      that subtree.

      The ``in_shardings`` argument is optional. JAX will infer the shardings
      from the input :py:class:`jax.Array`'s and defaults to replicating the input
      if the sharding cannot be inferred.

      The valid resource assignment specifications are:
        - :py:class:`Sharding`, which will decide how the value
            will be partitioned. With this, using a mesh context manager is not
            required.
        - :py:obj:`None`, will give JAX the freedom to choose whatever sharding
          it wants.
          For in_shardings, JAX will mark is as replicated but this behavior
          can change in the future.
          For out_shardings, we will rely on the XLA GSPMD partitioner to
          determine the output shardings.

      The size of every dimension has to be a multiple of the total number of
      resources assigned to it. This is similar to pjit's in_shardings.
    out_shardings: Like ``in_shardings``, but specifies resource
      assignment for function outputs. This is similar to pjit's
      out_shardings.

      The ``out_shardings`` argument is optional. If not specified, :py:func:`jax.jit`
      will use GSPMD's sharding propagation to figure out what the sharding of the
      output(s) should be.
    static_argnums: An optional int or collection of ints that specify which
      positional arguments to treat as static (compile-time constant).
      Operations that only depend on static arguments will be constant-folded in
      Python (during tracing), and so the corresponding argument values can be
      any Python object.

      Static arguments should be hashable, meaning both ``__hash__`` and
      ``__eq__`` are implemented, and immutable. Calling the jitted function
      with different values for these constants will trigger recompilation.
      Arguments that are not arrays or containers thereof must be marked as
      static.

      If neither ``static_argnums`` nor ``static_argnames`` is provided, no
      arguments are treated as static. If ``static_argnums`` is not provided but
      ``static_argnames`` is, or vice versa, JAX uses
      :code:`inspect.signature(fun)` to find any positional arguments that
      correspond to ``static_argnames``
      (or vice versa). If both ``static_argnums`` and ``static_argnames`` are
      provided, ``inspect.signature`` is not used, and only actual
      parameters listed in either ``static_argnums`` or ``static_argnames`` will
      be treated as static.
    static_argnames: An optional string or collection of strings specifying
      which named arguments to treat as static (compile-time constant). See the
      comment on ``static_argnums`` for details. If not
      provided but ``static_argnums`` is set, the default is based on calling
      ``inspect.signature(fun)`` to find corresponding named arguments.
    donate_argnums: Specify which positional argument buffers are "donated" to
      the computation. It is safe to donate argument buffers if you no longer
      need them once the computation has finished. In some cases XLA can make
      use of donated buffers to reduce the amount of memory needed to perform a
      computation, for example recycling one of your input buffers to store a
      result. You should not reuse buffers that you donate to a computation, JAX
      will raise an error if you try to. By default, no argument buffers are
      donated.

      If neither ``donate_argnums`` nor ``donate_argnames`` is provided, no
      arguments are donated. If ``donate_argnums`` is not provided but
      ``donate_argnames`` is, or vice versa, JAX uses
      :code:`inspect.signature(fun)` to find any positional arguments that
      correspond to ``donate_argnames``
      (or vice versa). If both ``donate_argnums`` and ``donate_argnames`` are
      provided, ``inspect.signature`` is not used, and only actual
      parameters listed in either ``donate_argnums`` or ``donate_argnames`` will
      be donated.

      For more details on buffer donation see the
      `FAQ <https://jax.readthedocs.io/en/latest/faq.html#buffer-donation>`_.
    donate_argnames: An optional string or collection of strings specifying
      which named arguments are donated to the computation. See the
      comment on ``donate_argnums`` for details. If not
      provided but ``donate_argnums`` is set, the default is based on calling
      ``inspect.signature(fun)`` to find corresponding named arguments.
    keep_unused: If `False` (the default), arguments that JAX determines to be
      unused by `fun` *may* be dropped from resulting compiled XLA executables.
      Such arguments will not be transferred to the device nor provided to the
      underlying executable. If `True`, unused arguments will not be pruned.
    device: This is an experimental feature and the API is likely to change.
      Optional, the Device the jitted function will run on. (Available devices
      can be retrieved via :py:func:`jax.devices`.) The default is inherited
      from XLA's DeviceAssignment logic and is usually to use
      ``jax.devices()[0]``.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the XLA backend: ``'cpu'``, ``'gpu'``, or
      ``'tpu'``.
    inline: Specify whether this function should be inlined into enclosing
      jaxprs (rather than being represented as an application of the xla_call
      primitive with its own subjaxpr). Default False.

  Returns:
    A wrapped version of ``fun``, set up for just-in-time compilation.
  """

  if fun is Missing:
    return functools.partial(
      jit,
      in_shardings=in_shardings,
      out_shardings=out_shardings,
      static_argnums=static_argnums,
      static_argnames=static_argnames,
      donate_argnums=donate_argnums,
      donate_argnames=donate_argnames,
      keep_unused=keep_unused,
      device=device,
      backend=backend,
      inline=inline,
      abstracted_axes=abstracted_axes,
    )  # type: ignore[return-value]
  kwarg_shardings = None
  jax_in_shardings = jax.tree.map(
    lambda x: extract.NodeStates.from_prefixes(x.shardings, metadata=x)
    if isinstance(x, StateSharding)
    else x,
    in_shardings,
  )
  jax_out_shardings = jax.tree.map(
    lambda x: extract.NodeStates.from_prefixes(x.shardings, metadata=x)
    if isinstance(x, StateSharding)
    else x,
    out_shardings,
  )

  jitted_fn = jax.jit(
    JitFn(fun, in_shardings, out_shardings, kwarg_shardings),
    in_shardings=jax_in_shardings,
    out_shardings=(jax_in_shardings, kwarg_shardings, jax_out_shardings),  # type: ignore
    static_argnums=static_argnums,
    static_argnames=static_argnames,
    donate_argnums=donate_argnums,
    donate_argnames=donate_argnames,
    keep_unused=keep_unused,
    device=device,
    backend=backend,
    inline=inline,
    abstracted_axes=abstracted_axes,
  )

  @functools.wraps(fun)
  @graph.update_context('jit')
  def jit_wrapper(*args, **kwargs):
    pure_args, pure_kwargs = extract.to_tree(
      (args, kwargs),
      prefix=(in_shardings, kwarg_shardings),
      split_fn=_jit_split_fn,
      check_aliasing=in_shardings is not None,
      ctxtag='jit',
    )
    pure_args_out, pure_kwargs_out, pure_out = jitted_fn(
      *pure_args, **pure_kwargs
    )
    _args_out, _kwargs_out, out = extract.from_tree(
      (pure_args_out, pure_kwargs_out, pure_out), ctxtag='jit'
    )
    return out

  jit_wrapper.inner = jitted_fn  # type: ignore

  return jit_wrapper  # type: ignore