import inspect

from functools import wraps
from typing import Callable, Dict, Type, Any, Optional, Tuple, TypeVar, overload, cast
from inspect import Signature, Parameter
from contextlib import nullcontext

from dlt.common.typing import DictStrAny, StrAny, TFun, AnyFun
from dlt.common.configuration.resolve import resolve_configuration, inject_section
from dlt.common.configuration.specs.base_configuration import BaseConfiguration
from dlt.common.configuration.specs.config_section_context import ConfigSectionContext

from dlt.common.reflection.spec import spec_from_signature


_LAST_DLT_CONFIG = "_dlt_config"
_ORIGINAL_ARGS = "_dlt_orig_args"
# keep a registry of all the decorated functions
_FUNC_SPECS: Dict[int, Type[BaseConfiguration]] = {}

TConfiguration = TypeVar("TConfiguration", bound=BaseConfiguration)


def get_fun_spec(f: AnyFun) -> Type[BaseConfiguration]:
    return _FUNC_SPECS.get(id(f))


@overload
def with_config(
    func: TFun,
    /,
    spec: Type[BaseConfiguration] = None,
    sections: Tuple[str, ...] = (),
    sections_merge_style: ConfigSectionContext.TMergeFunc = ConfigSectionContext.prefer_incoming,
    auto_pipeline_section: bool = False,
    include_defaults: bool = True,
    accept_partial: bool = False,
    initial_config: BaseConfiguration = None,
    base: Type[BaseConfiguration] = BaseConfiguration,
    lock_context_on_injection: bool = True,
) -> TFun: ...


@overload
def with_config(
    func: None = ...,
    /,
    spec: Type[BaseConfiguration] = None,
    sections: Tuple[str, ...] = (),
    sections_merge_style: ConfigSectionContext.TMergeFunc = ConfigSectionContext.prefer_incoming,
    auto_pipeline_section: bool = False,
    include_defaults: bool = True,
    accept_partial: bool = False,
    initial_config: Optional[BaseConfiguration] = None,
    base: Type[BaseConfiguration] = BaseConfiguration,
    lock_context_on_injection: bool = True,
) -> Callable[[TFun], TFun]: ...


def with_config(
    func: Optional[AnyFun] = None,
    /,
    spec: Type[BaseConfiguration] = None,
    sections: Tuple[str, ...] = (),
    sections_merge_style: ConfigSectionContext.TMergeFunc = ConfigSectionContext.prefer_incoming,
    auto_pipeline_section: bool = False,
    include_defaults: bool = True,
    accept_partial: bool = False,
    initial_config: Optional[BaseConfiguration] = None,
    base: Type[BaseConfiguration] = BaseConfiguration,
    lock_context_on_injection: bool = True,
) -> Callable[[TFun], TFun]:
    """Injects values into decorated function arguments following the specification in `spec` or by deriving one from function's signature.

    The synthesized spec contains the arguments marked with `dlt.secrets.value` and `dlt.config.value` which are required to be injected at runtime.
    Optionally (and by default) arguments with default values are included in spec as well.

    Args:
        func (Optional[AnyFun], optional): A function with arguments to be injected. Defaults to None.
        spec (Type[BaseConfiguration], optional): A specification of injectable arguments. Defaults to None.
        sections (Tuple[str, ...], optional): A set of config sections in which to look for arguments values. Defaults to ().
        prefer_existing_sections: (bool, optional): When joining existing section context, the existing context will be preferred to the one in `sections`. Default: False
        auto_pipeline_section (bool, optional): If True, a top level pipeline section will be added if `pipeline_name` argument is present . Defaults to False.
        include_defaults (bool, optional): If True then arguments with default values will be included in synthesized spec. If False only the required arguments marked with `dlt.secrets.value` and `dlt.config.value` are included
        base (Type[BaseConfiguration], optional): A base class for synthesized spec. Defaults to BaseConfiguration.
        lock_context_on_injection (bool, optional): If True, the thread context will be locked during injection to prevent race conditions. Defaults to True.
    Returns:
        Callable[[TFun], TFun]: A decorated function
    """

    section_f: Callable[[StrAny], str] = None
    # section may be a function from function arguments to section
    if callable(sections):
        section_f = sections

    def decorator(f: TFun) -> TFun:
        SPEC: Type[BaseConfiguration] = None
        sig: Signature = inspect.signature(f)
        signature_fields: Dict[str, Any]
        kwargs_arg = next(
            (p for p in sig.parameters.values() if p.kind == Parameter.VAR_KEYWORD), None
        )
        if spec is None:
            SPEC, signature_fields = spec_from_signature(f, sig, include_defaults, base=base)
        else:
            SPEC = spec
            signature_fields = SPEC.get_resolvable_fields()

        # if no signature fields were added we will not wrap `f` for injection
        if len(signature_fields) == 0:
            # always register new function
            _FUNC_SPECS[id(f)] = SPEC
            return f

        spec_arg: Parameter = None
        pipeline_name_arg: Parameter = None

        for p in sig.parameters.values():
            # for all positional parameters that do not have default value, set default
            # if hasattr(SPEC, p.name) and p.default == Parameter.empty:
            #     p._default = None  # type: ignore
            if p.annotation is SPEC:
                # if any argument has type SPEC then us it to take initial value
                spec_arg = p
            if p.name == "pipeline_name" and auto_pipeline_section:
                # if argument has name pipeline_name and auto_section is used, use it to generate section context
                pipeline_name_arg = p
                pipeline_name_arg_default = None if p.default == Parameter.empty else p.default

        def resolve_config(bound_args: inspect.BoundArguments) -> BaseConfiguration:
            """Resolve arguments using the provided spec"""
            # bind parameters to signature
            # for calls containing resolved spec in the kwargs, we do not need to resolve again
            config: BaseConfiguration = None

            # if section derivation function was provided then call it
            if section_f:
                curr_sections: Tuple[str, ...] = (section_f(bound_args.arguments),)
                # sections may be a string
            elif isinstance(sections, str):
                curr_sections = (sections,)
            else:
                curr_sections = sections

            # if one of arguments is spec the use it as initial value
            if initial_config:
                config = initial_config
            elif spec_arg:
                config = bound_args.arguments.get(spec_arg.name, None)
            # resolve SPEC, also provide section_context with pipeline_name
            if pipeline_name_arg:
                curr_pipeline_name = bound_args.arguments.get(
                    pipeline_name_arg.name, pipeline_name_arg_default
                )
            else:
                curr_pipeline_name = None
            section_context = ConfigSectionContext(
                pipeline_name=curr_pipeline_name,
                sections=curr_sections,
                merge_style=sections_merge_style,
            )

            # this may be called from many threads so section_context is thread affine
            with inject_section(section_context, lock_context=lock_context_on_injection):
                # print(f"RESOLVE CONF in inject: {f.__name__}: {section_context.sections} vs {sections}")
                return resolve_configuration(
                    config or SPEC(),
                    explicit_value=bound_args.arguments,
                    accept_partial=accept_partial,
                )

        def update_bound_args(
            bound_args: inspect.BoundArguments, config: BaseConfiguration, args: Any, kwargs: Any
        ) -> None:
            # overwrite or add resolved params
            resolved_params = dict(config)
            # overwrite or add resolved params
            for p in sig.parameters.values():
                if p.name in resolved_params:
                    bound_args.arguments[p.name] = resolved_params.pop(p.name)
                if p.annotation is SPEC:
                    bound_args.arguments[p.name] = config
            # pass all other config parameters into kwargs if present
            if kwargs_arg is not None:
                if kwargs_arg.name not in bound_args.arguments:
                    # add variadic keyword argument
                    bound_args.arguments[kwargs_arg.name] = {}
                bound_args.arguments[kwargs_arg.name].update(resolved_params)
                bound_args.arguments[kwargs_arg.name][_LAST_DLT_CONFIG] = config
                bound_args.arguments[kwargs_arg.name][_ORIGINAL_ARGS] = (args, kwargs)

        def with_partially_resolved_config(config: Optional[BaseConfiguration] = None) -> Any:
            # creates a pre-resolved partial of the decorated function
            empty_bound_args = sig.bind_partial()
            if not config:
                config = resolve_config(empty_bound_args)

            def wrapped(*args: Any, **kwargs: Any) -> Any:
                nonlocal config

                # Do we need an exception here?
                if spec_arg and spec_arg.name in kwargs:
                    from dlt.common import logger

                    logger.warning(
                        "Spec argument is provided in kwargs, ignoring it for resolved partial"
                        " function."
                    )

                # we can still overwrite the config
                if _LAST_DLT_CONFIG in kwargs:
                    config = last_config(**kwargs)

                # call the function with the pre-resolved config
                bound_args = sig.bind(*args, **kwargs)
                update_bound_args(bound_args, config, args, kwargs)
                return f(*bound_args.args, **bound_args.kwargs)

            return wrapped

        @wraps(f)
        def _wrap(*args: Any, **kwargs: Any) -> Any:
            # Resolve config
            config: BaseConfiguration = None
            bound_args = sig.bind(*args, **kwargs)
            if _LAST_DLT_CONFIG in kwargs:
                config = last_config(**kwargs)
            else:
                config = resolve_config(bound_args)

            # call the function with resolved config
            update_bound_args(bound_args, config, args, kwargs)
            return f(*bound_args.args, **bound_args.kwargs)

        # register the spec for a wrapped function
        _FUNC_SPECS[id(_wrap)] = SPEC

        # add a method to create a pre-resolved partial
        setattr(_wrap, "__RESOLVED_PARTIAL_FUNC__", with_partially_resolved_config)  # noqa: B010

        return _wrap  # type: ignore

    # See if we're being called as @with_config or @with_config().
    if func is None:
        # We're called with parens.
        return decorator

    if not callable(func):
        raise ValueError(
            "First parameter to the with_config must be callable ie. by using it as function"
            " decorator"
        )

    # We're called as @with_config without parens.
    return decorator(func)


def last_config(**kwargs: Any) -> Any:
    """Get configuration instance used to inject function arguments"""
    return kwargs[_LAST_DLT_CONFIG]


def get_orig_args(**kwargs: Any) -> Tuple[Tuple[Any], DictStrAny]:
    return kwargs[_ORIGINAL_ARGS]  # type: ignore


def create_resolved_partial(f: AnyFun, config: Optional[BaseConfiguration] = None) -> AnyFun:
    """Create a pre-resolved partial of the with_config decorated function"""
    if partial_func := getattr(f, "__RESOLVED_PARTIAL_FUNC__", None):
        return cast(AnyFun, partial_func(config))
    return f
