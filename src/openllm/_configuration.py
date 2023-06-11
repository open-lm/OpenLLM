# Copyright 2023 BentoML Team. All rights reserved.
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
"""
Configuration utilities for OpenLLM. All model configuration will inherit from ``openllm.LLMConfig``.

Highlight feature: Each fields in ``openllm.LLMConfig`` will also automatically generate a environment
variable based on its name field.

For example, the following config class:

```python
class FlanT5Config(openllm.LLMConfig):

    class GenerationConfig:
        temperature: float = 0.75
        max_new_tokens: int = 3000
        top_k: int = 50
        top_p: float = 0.4
        repetition_penalty = 1.0
```

which generates the environment OPENLLM_FLAN_T5_GENERATION_TEMPERATURE for users to configure temperature
dynamically during serve, ahead-of-serve or per requests.

Refer to ``openllm.LLMConfig`` docstring for more information.
"""
from __future__ import annotations

import inspect
import logging
import os
import typing as t
from operator import itemgetter

import attr
import inflection
import orjson
from cattr.gen import make_dict_unstructure_fn, override
from click_option_group import optgroup
from deepmerge.merger import Merger

import openllm

from .exceptions import GpuNotAvailableError, OpenLLMException
from .utils import LazyType, ModelEnv, bentoml_cattr, dantic, first_not_none, lenient_issubclass

if t.TYPE_CHECKING:
    import tensorflow as tf
    import torch
    import transformers
    from attr import _CountingAttr, _make_init, _make_method, _make_repr
    from transformers.generation.beam_constraints import Constraint

    from ._types import ClickFunctionWrapper, F, O_co, P

    ReprArgs: t.TypeAlias = t.Iterable[tuple[str | None, t.Any]]

    DictStrAny = dict[str, t.Any]
    ListStr = list[str]
    ItemgetterAny = itemgetter[t.Any]
else:
    Constraint = t.Any
    ListStr = list
    DictStrAny = dict
    ItemgetterAny = itemgetter
    # NOTE: Using internal API from attr here, since we are actually
    # allowing subclass of openllm.LLMConfig to become 'attrs'-ish
    from attr._make import _CountingAttr, _make_init, _make_method, _make_repr

    transformers = openllm.utils.LazyLoader("transformers", globals(), "transformers")
    torch = openllm.utils.LazyLoader("torch", globals(), "torch")
    tf = openllm.utils.LazyLoader("tf", globals(), "tensorflow")

__all__ = ["LLMConfig"]

logger = logging.getLogger(__name__)

config_merger = Merger(
    # merge dicts
    type_strategies=[(DictStrAny, "merge")],
    # override all other types
    fallback_strategies=["override"],
    # override conflicting types
    type_conflict_strategies=["override"],
)

_T = t.TypeVar("_T")


@t.overload
def attrs_to_options(
    name: str,
    field: attr.Attribute[t.Any],
    model_name: str,
    typ: type[t.Any] | None = None,
    suffix_generation: bool = False,
) -> F[..., F[..., openllm.LLMConfig]]:
    ...


@t.overload
def attrs_to_options(  # type: ignore (overlapping overload)
    name: str,
    field: attr.Attribute[O_co],
    model_name: str,
    typ: type[t.Any] | None = None,
    suffix_generation: bool = False,
) -> F[..., F[P, O_co]]:
    ...


def attrs_to_options(
    name: str,
    field: attr.Attribute[t.Any],
    model_name: str,
    typ: type[t.Any] | None = None,
    suffix_generation: bool = False,
) -> t.Callable[..., ClickFunctionWrapper[..., t.Any]]:
    # TODO: support parsing nested attrs class
    envvar = field.metadata["env"]
    dasherized = inflection.dasherize(name)
    underscored = inflection.underscore(name)

    if typ in (None, attr.NOTHING):
        typ = field.type

    full_option_name = f"--{dasherized}"
    if field.type is bool:
        full_option_name += f"/--no-{dasherized}"
    if suffix_generation:
        identifier = f"{model_name}_generation_{underscored}"
    else:
        identifier = f"{model_name}_{underscored}"

    return optgroup.option(
        identifier,
        full_option_name,
        type=dantic.parse_type(typ),
        required=field.default is attr.NOTHING,
        default=field.default if field.default not in (attr.NOTHING, None) else None,
        show_default=True,
        multiple=dantic.allows_multiple(typ),
        help=field.metadata.get("description", "(No description provided)"),
        show_envvar=True,
        envvar=envvar,
    )


@attr.define
class GenerationConfig:
    """Generation config provides the configuration to then be parsed to ``transformers.GenerationConfig``,
    with some additional validation and environment constructor.

    Note that we always set `do_sample=True`. This class is not designed to be used directly, rather
    to be used conjunction with LLMConfig. The instance of the generation config can then be accessed
    via ``LLMConfig.generation_config``.
    """

    # NOTE: parameters for controlling the length of the output
    max_new_tokens: int = dantic.Field(
        20,
        ge=0,
        description="The maximum numbers of tokens to generate, ignoring the number of tokens in the prompt.",
    )
    min_length: int = dantic.Field(
        0,
        ge=0,
        description="""The minimum length of the sequence to be generated. Corresponds to the length of the
        input prompt + `min_new_tokens`. Its effect is overridden by `min_new_tokens`, if also set.""",
    )
    min_new_tokens: int = dantic.Field(
        description="The minimum numbers of tokens to generate, ignoring the number of tokens in the prompt.",
    )
    early_stopping: bool = dantic.Field(
        False,
        description="""Controls the stopping condition for beam-based methods, like beam-search. It accepts the
        following values: `True`, where the generation stops as soon as there are `num_beams` complete candidates;
        `False`, where an heuristic is applied and the generation stops when is it very unlikely to find
        better candidates; `"never"`, where the beam search procedure only stops when there
        cannot be better candidates (canonical beam search algorithm)
    """,
    )
    max_time: float = dantic.Field(
        description="""The maximum amount of time you allow the computation to run for in seconds. generation will
        still finish the current pass after allocated time has been passed.""",
    )

    # NOTE: Parameters for controling generaiton strategies
    num_beams: int = dantic.Field(1, description="Number of beams for beam search. 1 means no beam search.")
    num_beam_groups: int = dantic.Field(
        1,
        description="""Number of groups to divide `num_beams` into in order to ensure diversity among different
        groups of beams. [this paper](https://arxiv.org/pdf/1610.02424.pdf) for more details.""",
    )
    penalty_alpha: float = dantic.Field(
        description="""The values balance the model confidence and the degeneration penalty in
        contrastive search decoding.""",
    )
    use_cache: bool = dantic.Field(
        True,
        description="""Whether or not the model should use the past last
        key/values attentions (if applicable to the model) to speed up decoding.""",
    )

    # NOTE: Parameters for manipulation of the model output logits
    temperature: float = dantic.Field(
        1.0, ge=0.0, le=1.0, description="The value used to modulate the next token probabilities."
    )
    top_k: int = dantic.Field(
        50, description="The number of highest probability vocabulary tokens to keep for top-k-filtering."
    )
    top_p: float = dantic.Field(
        1.0,
        description="""If set to float < 1, only the smallest set of most probable tokens with
        probabilities that add up to `top_p` or higher are kept for generation.""",
    )
    typical_p: float = dantic.Field(
        1.0,
        description="""Local typicality measures how similar the conditional probability of predicting a target
        token next is to the expected conditional probability of predicting a random token next, given the
        partial text already generated. If set to float < 1, the smallest set of the most locally typical
        tokens with probabilities that add up to `typical_p` or higher are kept for generation. See [this
        paper](https://arxiv.org/pdf/2202.00666.pdf) for more details.
    """,
    )
    epsilon_cutoff: float = dantic.Field(
        0.0,
        description="""\
        If set to float strictly between 0 and 1, only tokens with a conditional probability greater than
        `epsilon_cutoff` will be sampled. In the paper, suggested values range from 3e-4 to 9e-4, depending on the
        size of the model. See [Truncation Sampling as Language Model Desmoothing](https://arxiv.org/abs/2210.15191)
        for more details.
    """,
    )
    eta_cutoff: float = dantic.Field(
        0.0,
        description="""Eta sampling is a hybrid of locally typical sampling and epsilon sampling.
        If set to float strictly between 0 and 1, a token is only considered if it is greater than
        either `eta_cutoff` or `sqrt(eta_cutoff) * exp(-entropy(softmax(next_token_logits)))`. The latter term is
        intuitively the expected next token probability, scaled by `sqrt(eta_cutoff)`. In the paper, suggested
        values range from 3e-4 to 2e-3, depending on the size of the model.
        See [Truncation Sampling as Language Model Desmoothing](https://arxiv.org/abs/2210.15191) for more details.
    """,
    )
    diversity_penalty: float = dantic.Field(
        0.0,
        description="""This value is subtracted from a beam's score if it generates a token same
        as any beam from other group at a particular time. Note that `diversity_penalty` is only
        effective if `group beam search` is enabled.
    """,
    )
    repetition_penalty: float = dantic.Field(
        1.0,
        description="""The parameter for repetition penalty. 1.0 means no penalty.
        See [this paper](https://arxiv.org/pdf/1909.05858.pdf) for more details.""",
    )
    encoder_repetition_penalty: float = dantic.Field(
        1.0,
        description="""The paramater for encoder_repetition_penalty. An exponential penalty on sequences that are
        not in the original input. 1.0 means no penalty.""",
    )
    length_penalty: float = dantic.Field(
        1.0,
        description="""Exponential penalty to the length that is used with beam-based generation. It is applied
        as an exponent to the sequence length, which in turn is used to divide the score of the sequence. Since
        the score is the log likelihood of the sequence (i.e. negative), `length_penalty` > 0.0 promotes longer
        sequences, while `length_penalty` < 0.0 encourages shorter sequences.
    """,
    )
    no_repeat_ngram_size: int = dantic.Field(
        0, description="If set to int > 0, all ngrams of that size can only occur once."
    )
    bad_words_ids: t.List[t.List[int]] = dantic.Field(
        description="""List of token ids that are not allowed to be generated. In order to get the token ids
        of the words that should not appear in the generated text, use
        `tokenizer(bad_words, add_prefix_space=True, add_special_tokens=False).input_ids`.
        """,
    )

    # NOTE: t.Union is not yet supported on CLI, but the environment variable should already be available.
    force_words_ids: t.Union[t.List[t.List[int]], t.List[t.List[t.List[int]]]] = dantic.Field(
        description="""List of token ids that must be generated. If given a `List[List[int]]`, this is treated
        as a simple list of words that must be included, the opposite to `bad_words_ids`.
        If given `List[List[List[int]]]`, this triggers a
        [disjunctive constraint](https://github.com/huggingface/transformers/issues/14081), where one
        can allow different forms of each word.
        """,
    )
    renormalize_logits: bool = dantic.Field(
        False,
        description="""Whether to renormalize the logits after applying all the logits processors or warpers
        (including the custom ones). It's highly recommended to set this flag to `True` as the search
        algorithms suppose the score logits are normalized but some logit processors or warpers break the normalization.
    """,
    )
    constraints: t.List[Constraint] = dantic.Field(
        description="""Custom constraints that can be added to the generation to ensure that the output
        will contain the use of certain tokens as defined by ``Constraint`` objects, in the most sensible way possible.
        """,
    )
    forced_bos_token_id: int = dantic.Field(
        description="""The id of the token to force as the first generated token after the
        ``decoder_start_token_id``. Useful for multilingual models like
        [mBART](https://huggingface.co/docs/transformers/model_doc/mbart) where the first generated token needs
        to be the target language token.
    """,
    )
    forced_eos_token_id: t.Union[int, t.List[int]] = dantic.Field(
        description="""The id of the token to force as the last generated token when `max_length` is reached.
        Optionally, use a list to set multiple *end-of-sequence* tokens.""",
    )
    remove_invalid_values: bool = dantic.Field(
        False,
        description="""Whether to remove possible *nan* and *inf* outputs of the model to prevent the
        generation method to crash. Note that using `remove_invalid_values` can slow down generation.""",
    )
    exponential_decay_length_penalty: t.Tuple[int, float] = dantic.Field(
        description="""This tuple adds an exponentially increasing length penalty, after a certain amount of tokens
        have been generated. The tuple shall consist of: `(start_index, decay_factor)` where `start_index`
        indicates where penalty starts and `decay_factor` represents the factor of exponential decay
    """,
    )
    suppress_tokens: t.List[int] = dantic.Field(
        description="""A list of tokens that will be suppressed at generation. The `SupressTokens` logit
        processor will set their log probs to `-inf` so that they are not sampled.
    """,
    )
    begin_suppress_tokens: t.List[int] = dantic.Field(
        description="""A list of tokens that will be suppressed at the beginning of the generation. The
        `SupressBeginTokens` logit processor will set their log probs to `-inf` so that they are not sampled.
        """,
    )
    forced_decoder_ids: t.List[t.List[int]] = dantic.Field(
        description="""A list of pairs of integers which indicates a mapping from generation indices to token indices
        that will be forced before sampling. For example, `[[1, 123]]` means the second generated token will always
        be a token of index 123.
        """,
    )

    # NOTE: Parameters that define the output variables of `generate`
    num_return_sequences: int = dantic.Field(
        1, description="The number of independently computed returned sequences for each element in the batch."
    )
    output_attentions: bool = dantic.Field(
        False,
        description="""Whether or not to return the attentions tensors of all attention layers.
        See `attentions` under returned tensors for more details. """,
    )
    output_hidden_states: bool = dantic.Field(
        False,
        description="""Whether or not to return the hidden states of all layers.
        See `hidden_states` under returned tensors for more details.
        """,
    )
    output_scores: bool = dantic.Field(
        False,
        description="""Whether or not to return the prediction scores. See `scores` under returned
        tensors for more details.""",
    )

    # NOTE: Special tokens that can be used at generation time
    pad_token_id: int = dantic.Field(description="The id of the *padding* token.")
    bos_token_id: int = dantic.Field(description="The id of the *beginning-of-sequence* token.")
    eos_token_id: t.Union[int, t.List[int]] = dantic.Field(
        description="""The id of the *end-of-sequence* token. Optionally, use a list to set
        multiple *end-of-sequence* tokens.""",
    )

    # NOTE: Generation parameters exclusive to encoder-decoder models
    encoder_no_repeat_ngram_size: int = dantic.Field(
        0,
        description="""If set to int > 0, all ngrams of that size that occur in the
        `encoder_input_ids` cannot occur in the `decoder_input_ids`.
        """,
    )
    decoder_start_token_id: int = dantic.Field(
        description="""If an encoder-decoder model starts decoding with a
        different token than *bos*, the id of that token.
        """,
    )

    if t.TYPE_CHECKING:

        def __attrs_init__(self, **_: t.Any):
            ...

    def __init__(self, *, _internal: bool = False, **attrs: t.Any):
        if not _internal:
            raise RuntimeError(
                "GenerationConfig is not meant to be used directly, "
                "but you can access this via a LLMConfig.generation_config"
            )
        self.__attrs_init__(**attrs)


bentoml_cattr.register_unstructure_hook_factory(
    lambda cls: attr.has(cls) and lenient_issubclass(cls, GenerationConfig),
    lambda cls: make_dict_unstructure_fn(
        cls,
        bentoml_cattr,
        **{k: override(omit=True) for k, v in attr.fields_dict(cls).items() if v.default in (None, attr.NOTHING)},
    ),
)


def _populate_value_from_env_var(
    key: str, transform: t.Callable[[str], str] | None = None, fallback: t.Any = None
) -> t.Any:
    if transform is not None and callable(transform):
        key = transform(key)

    return os.environ.get(key, fallback)


# sentinel object for unequivocal object() getattr
_sentinel = object()


def _has_own_attribute(cls: type[t.Any], attrib_name: t.Any):
    """
    Check whether *cls* defines *attrib_name* (and doesn't just inherit it).
    """
    attr = getattr(cls, attrib_name, _sentinel)
    if attr is _sentinel:
        return False

    for base_cls in cls.__mro__[1:]:
        a = getattr(base_cls, attrib_name, None)
        if attr is a:
            return False

    return True


def _get_annotations(cls: type[t.Any]) -> DictStrAny:
    """
    Get annotations for *cls*.
    """
    if _has_own_attribute(cls, "__annotations__"):
        return cls.__annotations__

    return DictStrAny()


# The below is vendorred from attrs
def _collect_base_attrs(
    cls: type[LLMConfig], taken_attr_names: set[str]
) -> tuple[list[attr.Attribute[t.Any]], dict[str, type[t.Any]]]:
    """
    Collect attr.ibs from base classes of *cls*, except *taken_attr_names*.
    """
    base_attrs: list[attr.Attribute[t.Any]] = []
    base_attr_map: dict[str, type[t.Any]] = {}  # A dictionary of base attrs to their classes.

    # Traverse the MRO and collect attributes.
    for base_cls in reversed(cls.__mro__[1:-1]):
        for a in getattr(base_cls, "__attrs_attrs__", []):
            if a.inherited or a.name in taken_attr_names:
                continue

            a = a.evolve(inherited=True)
            base_attrs.append(a)
            base_attr_map[a.name] = base_cls

    # For each name, only keep the freshest definition i.e. the furthest at the back.
    filtered: list[attr.Attribute[t.Any]] = []
    seen: set[str] = set()
    for a in reversed(base_attrs):
        if a.name in seen:
            continue
        filtered.insert(0, a)
        seen.add(a.name)

    return filtered, base_attr_map


_classvar_prefixes = (
    "typing.ClassVar",
    "t.ClassVar",
    "ClassVar",
    "typing_extensions.ClassVar",
)


def _is_class_var(annot: str | t.Any) -> bool:
    """
    Check whether *annot* is a typing.ClassVar.

    The string comparison hack is used to avoid evaluating all string
    annotations which would put attrs-based classes at a performance
    disadvantage compared to plain old classes.
    """
    annot = str(annot)

    # Annotation can be quoted.
    if annot.startswith(("'", '"')) and annot.endswith(("'", '"')):
        annot = annot[1:-1]

    return annot.startswith(_classvar_prefixes)


def _add_method_dunders(cls: type[t.Any], method: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
    """
    Add __module__ and __qualname__ to a *method* if possible.
    """
    try:
        method.__module__ = cls.__module__
    except AttributeError:
        pass

    try:
        method.__qualname__ = ".".join((cls.__qualname__, method.__name__))
    except AttributeError:
        pass

    try:
        method.__doc__ = "Method generated by attrs for class " f"{cls.__qualname__}."
    except AttributeError:
        pass

    return method


# NOTE: vendorred from attrs
def _compile_and_eval(script: str, globs: dict[str, t.Any], locs: dict[str, t.Any] | None = None, filename: str = ""):
    """
    "Exec" the script with the given global (globs) and local (locs) variables.
    """
    bytecode = compile(script, filename, "exec")
    eval(bytecode, globs, locs)


def _make_attr_tuple_class(cls_name: str, attr_names: t.Iterable[str]) -> type[tuple[attr.Attribute[t.Any], ...]]:
    """
    Create a tuple subclass to hold `Attribute`s for an `attrs` class.

    The subclass is a bare tuple with properties for names.

    class MyClassAttributes(tuple):
        __slots__ = ()
        x = property(itemgetter(0))
    """
    attr_class_name = f"{cls_name}Attributes"
    attr_class_template = [
        f"class {attr_class_name}(tuple):",
        "    __slots__ = ()",
    ]
    if attr_names:
        for i, attr_name in enumerate(attr_names):
            attr_class_template.append(f"    {attr_name} = _attrs_property(_attrs_itemgetter({i}))")
    else:
        attr_class_template.append("    pass")
    globs: dict[str, t.Any] = {"_attrs_itemgetter": ItemgetterAny, "_attrs_property": property}
    _compile_and_eval("\n".join(attr_class_template), globs)
    return globs[attr_class_name]


def _make_internal_generation_class(cls: type[LLMConfig]) -> type[GenerationConfig]:
    _has_gen_class = _has_own_attribute(cls, "GenerationConfig")

    def _evolve_with_base_default(
        _: type[GenerationConfig], fields: list[attr.Attribute[t.Any]]
    ) -> list[attr.Attribute[t.Any]]:
        transformed: list[attr.Attribute[t.Any]] = []
        for f in fields:
            env = f"OPENLLM_{cls.__openllm_model_name__.upper()}_GENERATION_{f.name.upper()}"
            _from_env = _populate_value_from_env_var(env, fallback=f.default)
            default_value = f.default if not _has_gen_class else getattr(cls.GenerationConfig, f.name, _from_env)
            transformed.append(
                f.evolve(default=default_value, metadata={"env": env, "description": f.metadata.get("description")})
            )
        return transformed

    _cl = attr.make_class(
        cls.__name__.replace("Config", "GenerationConfig"),
        [],
        bases=(GenerationConfig,),
        frozen=True,
        slots=True,
        repr=True,
        field_transformer=_evolve_with_base_default,
    )
    _cl.__doc__ = GenerationConfig.__doc__

    if _has_gen_class:
        delattr(cls, "GenerationConfig")

    return _cl


# NOTE: This is the ModelConfig where we can control the behaviour of the LLM.
# refers to the __openllm_*__ docstring inside LLMConfig for more information.
class ModelConfig(t.TypedDict, total=False):
    # NOTE: meta
    url: str
    requires_gpu: bool
    trust_remote_code: bool
    requirements: t.Optional[t.List[str]]

    # NOTE: naming convention, only name_type is needed
    # as the three below it can be determined automatically
    name_type: t.Literal["dasherize", "lowercase"]
    model_name: str
    start_name: str
    env: openllm.utils.ModelEnv

    # NOTE: serving configuration
    timeout: int
    workers_per_resource: t.Union[int, float]

    # NOTE: use t.Required once we drop 3.8 support
    default_id: str
    model_ids: list[str]


def _gen_default_model_config(cls: type[LLMConfig]) -> ModelConfig:
    """Generate the default ModelConfig and delete __config__ in LLMConfig
    if defined inplace."""

    _internal_config = t.cast(ModelConfig, getattr(cls, "__config__", {}))
    default_id = _internal_config.get("default_id", None)
    if default_id is None:
        raise RuntimeError("'default_id' is required under '__config__'.")
    model_ids = _internal_config.get("model_ids", None)
    if model_ids is None:
        raise RuntimeError("'model_ids' is required under '__config__'.")

    def _first_not_null(key: str, default: _T) -> _T:
        return first_not_none(_internal_config.get(key), default=default)

    llm_config_striped = cls.__name__.replace("Config", "")

    name_type: t.Literal["dasherize", "lowercase"] = _first_not_null("name_type", "dasherize")

    if name_type == "dasherize":
        default_model_name = inflection.underscore(llm_config_striped)
        default_start_name = inflection.dasherize(default_model_name)
    else:
        default_model_name = llm_config_striped.lower()
        default_start_name = default_model_name

    model_name = _first_not_null("model_name", default_model_name)

    _config = ModelConfig(
        name_type=name_type,
        model_name=model_name,
        default_id=default_id,
        model_ids=model_ids,
        start_name=_first_not_null("start_name", default_start_name),
        url=_first_not_null("url", "(not provided)"),
        requires_gpu=_first_not_null("requires_gpu", False),
        trust_remote_code=_first_not_null("trust_remote_code", False),
        requirements=_first_not_null("requirements", ListStr()),
        env=_first_not_null("env", openllm.utils.ModelEnv(model_name)),
        timeout=_first_not_null("timeout", 3600),
        workers_per_resource=_first_not_null("workers_per_resource", 1),
    )

    if hasattr(cls, "__config__"):
        delattr(cls, "__config__")

    return _config


def _generate_unique_filename(cls: type[t.Any], func_name: str):
    return f"<LLMConfig generated {func_name} {cls.__module__}." f"{getattr(cls, '__qualname__', cls.__name__)}>"


def _setattr_class(attr_name: str, value_var: t.Any):
    """
    Use the builtin setattr to set *attr_name* to *value_var*.
    We can't use the cached object.__setattr__ since we are setting
    attributes to a class.
    """
    return f"setattr(cls, '{attr_name}', {value_var})"


@t.overload
def _make_assignment_with_prefix_script(cls: type[LLMConfig], attributes: ModelConfig) -> t.Callable[..., None]:
    ...


@t.overload
def _make_assignment_with_prefix_script(cls: type[LLMConfig], attributes: dict[str, t.Any]) -> t.Callable[..., None]:
    ...


def _make_assignment_with_prefix_script(cls: type[LLMConfig], attributes: t.Any) -> t.Callable[..., None]:
    """Generate the assignment script with prefix attributes __openllm_<value>__"""
    args: list[str] = []
    globs: dict[str, t.Any] = {"cls": cls, "attr_dict": attributes}
    annotations: dict[str, t.Any] = {"return": None}

    # Circumvent __setattr__ descriptor to save one lookup per assigment
    lines: list[str] = []
    for attr_name in attributes:
        arg_name = f"__openllm_{inflection.underscore(attr_name)}__"
        args.append(f"{attr_name}=attr_dict['{attr_name}']")
        lines.append(_setattr_class(arg_name, attr_name))
        annotations[attr_name] = type(attributes[attr_name])

    script = "def __assign_attr(cls, %s):\n    %s\n" % (", ".join(args), "\n    ".join(lines) if lines else "pass")
    assign_method = _make_method(
        "__assign_attr",
        script,
        _generate_unique_filename(cls, "__assign_attr"),
        globs,
    )
    assign_method.__annotations__ = annotations

    return assign_method


@attr.define
class LLMConfig:
    """
    ``openllm.LLMConfig`` is somewhat a hybrid combination between the performance of `attrs` with the
    easy-to-use interface that pydantic offer. It lives in between where it allows users to quickly formulate
    a LLMConfig for any LLM without worrying too much about performance. It does a few things:

    - Automatic environment conversion: Each fields will automatically be provisioned with an environment
        variable, make it easy to work with ahead-of-time or during serving time
    - Familiar API: It is compatible with cattrs as well as providing a few Pydantic-2 like API,
        i.e: ``model_construct_env``, ``to_generation_config``, ``to_click_options``
    - Automatic CLI generation: It can identify each fields and convert it to compatible Click options.
        This means developers can use any of the LLMConfig to create CLI with compatible-Python
        CLI library (click, typer, ...)

    > Internally, LLMConfig is an attrs class. All subclass of LLMConfig contains "attrs-like" features,
    > which means LLMConfig will actually generate subclass to have attrs-compatible API, so that the subclass
    > can be written as any normal Python class.

    To directly configure GenerationConfig for any given LLM, create a GenerationConfig under the subclass:

    ```python
    class FlanT5Config(openllm.LLMConfig):

        class GenerationConfig:
            temperature: float = 0.75
            max_new_tokens: int = 3000
            top_k: int = 50
            top_p: float = 0.4
            repetition_penalty = 1.0
    ```
    By doing so, openllm.LLMConfig will create a compatible GenerationConfig attrs class that can be converted
    to ``transformers.GenerationConfig``. These attribute can be accessed via ``LLMConfig.generation_config``.

    By default, all LLMConfig has a __config__ that contains a default value. If any LLM requires customization,
    provide a ``__config__`` under the class declaration:

    ```python
    class FalconConfig(openllm.LLMConfig):
        __config__ = {"trust_remote_code": True, "default_timeout": 3600000}
    ```

    Note that ``model_name``, ``start_name``, and ``env`` is optional under ``__config__``. If set, then OpenLLM
    will respect that option for start and other components within the library.
    """

    Field = dantic.Field
    """Field is a alias to the internal dantic utilities to easily create
    attrs.fields with pydantic-compatible interface. For example:

    ```python
    class MyModelConfig(openllm.LLMConfig):

        field1 = openllm.LLMConfig.Field(...)
    ```
    """

    # NOTE: The following is handled via __init_subclass__, and is only used for TYPE_CHECKING
    if t.TYPE_CHECKING:
        # NOTE: Internal attributes that should only be used by OpenLLM. Users usually shouldn't
        # concern any of these.
        def __attrs_init__(self, **attrs: t.Any):
            """Generated __attrs_init__ for LLMConfig subclass that follows the attrs contract."""

        __config__: ModelConfig | None = None
        """Internal configuration for this LLM model. Each of the field in here will be populated
        and prefixed with __openllm_<value>__"""

        __attrs_attrs__: tuple[attr.Attribute[t.Any], ...] = tuple()
        """Since we are writing our own __init_subclass__, which is an alternative way for __prepare__,
        we want openllm.LLMConfig to be attrs-like dataclass that has pydantic-like interface.
        __attrs_attrs__ will be handled dynamically by __init_subclass__.
        """

        __openllm_attrs__: tuple[str, ...] = tuple()
        """Internal attribute tracking to store converted LLMConfig attributes to correct attrs"""

        __openllm_hints__: dict[str, t.Any] = Field(None, init=False)
        """An internal cache of resolved types for this LLMConfig."""

        __openllm_accepted_keys__: set[str] = Field(None, init=False)
        """The accepted keys for this LLMConfig."""

        # NOTE: The following will be populated from __config__
        __openllm_url__: str = Field(None, init=False)
        """The resolved url for this LLMConfig."""

        __openllm_requires_gpu__: bool = False
        """Determines if this model is only available on GPU. By default it supports GPU and fallback to CPU."""

        __openllm_trust_remote_code__: bool = False
        """Whether to always trust remote code"""

        __openllm_requirements__: list[str] | None = None
        """The default PyPI requirements needed to run this given LLM. By default, we will depend on
        bentoml, torch, transformers."""

        __openllm_env__: openllm.utils.ModelEnv = Field(None, init=False)
        """A ModelEnv instance for this LLMConfig."""

        __openllm_model_name__: str = ""
        """The normalized version of __openllm_start_name__, determined by __openllm_name_type__"""

        __openllm_start_name__: str = ""
        """Default name to be used with `openllm start`"""

        __openllm_name_type__: t.Literal["dasherize", "lowercase"] = "dasherize"
        """the default name typed for this model. "dasherize" will convert the name to lowercase and
        replace spaces with dashes. "lowercase" will convert the name to lowercase."""

        __openllm_timeout__: int = 3600
        """The default timeout to be set for this given LLM."""

        __openllm_workers_per_resource__: int | float = 1
        """The default number of workers per resource. By default, we will use 1 worker per resource.
        See StarCoder for more advanced usage. See
        https://docs.bentoml.org/en/latest/guides/scheduling.html#resource-scheduling-strategy for more details.
        """

        __openllm_default_id__: str = Field(None)
        """Return the default model to use when using 'openllm start <model_id>'.
        This could be one of the keys in 'self.model_ids' or custom users model."""

        __openllm_model_ids__: list[str] = Field(None)
        """A list of supported pretrained models tag for this given runnable.

        For example:
            For FLAN-T5 impl, this would be ["google/flan-t5-small", "google/flan-t5-base",
                                                "google/flan-t5-large", "google/flan-t5-xl", "google/flan-t5-xxl"]
        """

        GenerationConfig: type = type
        """Users can override this subclass of any given LLMConfig to provide GenerationConfig
        default value. For example:

        ```python
        class MyAwesomeModelConfig(openllm.LLMConfig):
            class GenerationConfig:
                max_new_tokens: int = 200
                top_k: int = 10
                num_return_sequences: int = 1
                eos_token_id: int = 11
        ```
        """

        generation_class: type[GenerationConfig] = Field(None, init=False)
        """The result generated GenerationConfig class for this LLMConfig. This will be used
        to create the generation_config argument that can be used throughout the lifecycle."""

    def __init_subclass__(cls):
        # NOTE: auto assignment attributes generated from __config__
        _make_assignment_with_prefix_script(cls, _gen_default_model_config(cls))(cls)

        # NOTE: Since we want to enable a pydantic-like experience
        # this means we will have to hide the attr abstraction, and generate
        # all of the Field from __init_subclass__
        # Some of the logics here are from attr._make._transform_attrs
        anns = _get_annotations(cls)
        cd = cls.__dict__

        def field_env_key(key: str) -> str:
            return f"OPENLLM_{cls.__openllm_model_name__.upper()}_{key.upper()}"

        ca_names = {name for name, attr in cd.items() if isinstance(attr, _CountingAttr)}
        ca_list: list[tuple[str, _CountingAttr[t.Any]]] = []
        annotated_names: set[str] = set()
        for attr_name, typ in anns.items():
            if _is_class_var(typ):
                continue
            annotated_names.add(attr_name)
            val = cd.get(attr_name, attr.NOTHING)
            if not LazyType["_CountingAttr[t.Any]"](_CountingAttr).isinstance(val):
                if val is attr.NOTHING:
                    val = cls.Field(env=field_env_key(attr_name))
                else:
                    val = cls.Field(default=val, env=field_env_key(attr_name))
            ca_list.append((attr_name, val))
        unannotated = ca_names - annotated_names

        if len(unannotated) > 0:
            missing_annotated = sorted(unannotated, key=lambda n: t.cast("_CountingAttr[t.Any]", cd.get(n)).counter)
            raise openllm.exceptions.MissingAnnotationAttributeError(
                f"The following field doesn't have a type annotation: {missing_annotated}"
            )

        hints = t.get_type_hints(cls)
        # NOTE: we know need to determine the list of the attrs
        # by mro to at the very least support inheritance. Tho it is not recommended.
        own_attrs: list[attr.Attribute[t.Any]] = []
        for attr_name, ca in ca_list:
            gen_attribute = attr.Attribute.from_counting_attr(name=attr_name, ca=ca, type=hints.get(attr_name))
            if attr_name in ca_names:
                metadata = ca.metadata
                metadata["env"] = field_env_key(attr_name)
                gen_attribute = gen_attribute.evolve(metadata=metadata)
            own_attrs.append(gen_attribute)

        # This is to handle subclass of subclass of all provided LLMConfig.
        # refer to attrs for the original implementation.
        base_attrs, base_attr_map = _collect_base_attrs(cls, {a.name for a in own_attrs})

        # __openllm_attrs__ is a tracking tuple[attr.Attribute[t.Any]]
        # that we construct ourself.
        cls.__openllm_attrs__ = tuple(a.name for a in own_attrs)

        attrs: list[attr.Attribute[t.Any]] = own_attrs + base_attrs

        # Mandatory vs non-mandatory attr order only matters when they are part of
        # the __init__ signature and when they aren't kw_only (which are moved to
        # the end and can be mandatory or non-mandatory in any order, as they will
        # be specified as keyword args anyway). Check the order of those attrs:
        had_default = False
        for a in (a for a in attrs if a.init is not False and a.kw_only is False):
            if had_default is True and a.default is attr.NOTHING:
                raise ValueError(
                    "No mandatory attributes allowed after an attribute with a "
                    f"default value or factory.  Attribute in question: {a!r}"
                )

            if had_default is False and a.default is not attr.NOTHING:
                had_default = True

        # NOTE: Resolve the alias and default value from environment variable
        attrs = [
            a.evolve(
                alias=a.name.lstrip("_") if not a.alias else None,
                # NOTE: This is where we actually populate with the environment variable set for this attrs.
                default=_populate_value_from_env_var(a.name, transform=field_env_key, fallback=a.default),
            )
            for a in attrs
        ]

        _has_pre_init = bool(getattr(cls, "__attrs_pre_init__", False))
        _has_post_init = bool(getattr(cls, "__attrs_post_init__", False))

        AttrsTuple = _make_attr_tuple_class(cls.__name__, cls.__openllm_attrs__)
        # NOTE: the protocol for attrs-decorated class
        cls.__attrs_attrs__ = AttrsTuple(attrs)
        # NOTE: generate a __attrs_init__ for the subclass
        cls.__attrs_init__ = _add_method_dunders(
            cls,
            _make_init(
                cls,  # cls (the attrs-decorated class)
                cls.__attrs_attrs__,  # tuple of attr.Attribute of cls
                _has_pre_init,  # pre_init
                _has_post_init,  # post_init
                False,  # frozen
                True,  # slots
                True,  # cache_hash
                base_attr_map,  # base_attr_map
                False,  # is_exc (check if it is exception)
                None,  # cls_on_setattr (essentially attr.setters)
                attrs_init=True,  # whether to create __attrs_init__ instead of __init__
            ),
        )

        # NOTE: Finally, set the generation_class for this given config.
        cls.generation_class = _make_internal_generation_class(cls)

        hints.update(t.get_type_hints(cls.generation_class))
        cls.__openllm_hints__ = hints
        cls.__openllm_accepted_keys__ = set(cls.__openllm_attrs__) | set(attr.fields_dict(cls.generation_class))

    def __init__(
        self,
        *,
        generation_config: dict[str, t.Any] | None = None,
        __openllm_extras__: dict[str, t.Any] | None = None,
        **attrs: t.Any,
    ):
        # create a copy of the list of keys as cache
        _cached_keys = tuple(attrs.keys())

        self.__openllm_extras__ = first_not_none(__openllm_extras__, default={})
        config_merger.merge(
            self.__openllm_extras__, {k: v for k, v in attrs.items() if k not in self.__openllm_accepted_keys__}
        )

        for k in _cached_keys:
            if k in self.__openllm_extras__ or attrs.get(k) is None:
                del attrs[k]
        _cached_keys = tuple(k for k in _cached_keys if k in attrs)

        _generation_cl_dict = attr.fields_dict(self.generation_class)
        if generation_config is None:
            generation_config = {k: v for k, v in attrs.items() if k in _generation_cl_dict}
        else:
            generation_keys = {k for k in attrs if k in _generation_cl_dict}
            if len(generation_keys) > 0:
                logger.warning(
                    "When 'generation_config' is passed, \
                the following keys are ignored and won't be used: %s. If you wish to use those values, \
                pass it into 'generation_config'.",
                    ", ".join(generation_keys),
                )
            for k in _cached_keys:
                if k in generation_keys:
                    del attrs[k]
            _cached_keys = tuple(k for k in _cached_keys if k in attrs)

        self.generation_config = self.generation_class(**generation_config)
        base_attrs: tuple[attr.Attribute[t.Any], ...] = attr.fields(self.__class__)
        base_attrs += (
            attr.Attribute.from_counting_attr(
                name="generation_config",
                ca=dantic.Field(
                    self.generation_config, description=inspect.cleandoc(self.generation_class.__doc__ or "")
                ),
                type=self.generation_class,
            ),
        )
        # mk the class __repr__ function with the updated fields.
        self.__class__.__repr__ = _add_method_dunders(self.__class__, _make_repr(base_attrs, None, self.__class__))

        for k in _cached_keys:
            if k in generation_config:
                del attrs[k]

        # The rest of attrs should only be the attributes to be passed to __attrs_init__
        self.__attrs_init__(**attrs)

    def __getattr__(self, item: str) -> t.Any:
        if hasattr(self.generation_config, item):
            return getattr(self.generation_config, item)
        elif item in self.__openllm_extras__:
            return self.__openllm_extras__[item]
        else:
            return super().__getattribute__(item)

    @classmethod
    def check_if_gpu_is_available(cls, implementation: t.Literal["pt", "tf", "flax"] = "pt", force: bool = False):
        try:
            if cls.__openllm_requires_gpu__ or force:
                if implementation in ("tf", "flax") and len(tf.config.list_physical_devices("GPU")) == 0:
                    raise OpenLLMException("Required GPU for given model")
                else:
                    if not torch.cuda.is_available():
                        raise OpenLLMException("Required GPU for given model")
            else:
                logger.debug(
                    f"{cls} doesn't requires GPU by default. If you still want to check for GPU, set 'force=True'"
                )
        except OpenLLMException:
            if force:
                msg = "GPU is not available"
            else:
                msg = f"{cls} only supports running with GPU (None available)."
            raise GpuNotAvailableError(msg) from None

    def model_dump(self, flatten: bool = False, **_: t.Any):
        dumped = bentoml_cattr.unstructure(self)
        generation_config = bentoml_cattr.unstructure(self.generation_config)
        if not flatten:
            dumped["generation_config"] = generation_config
        else:
            dumped.update(generation_config)
        return dumped

    def model_dump_json(self, **kwargs: t.Any):
        return orjson.dumps(self.model_dump(**kwargs))

    @classmethod
    def model_construct_env(cls, **attrs: t.Any) -> LLMConfig:
        """A helpers that respect configuration values that
        sets from environment variables for any given configuration class.
        """
        attrs = {k: v for k, v in attrs.items() if v is not None}

        model_config = ModelEnv(cls.__openllm_model_name__).model_config

        env_json_string = os.environ.get(model_config, None)

        if env_json_string is not None:
            try:
                config_from_env = orjson.loads(env_json_string)
            except orjson.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse '{model_config}' as valid JSON string.") from e
            ncls = bentoml_cattr.structure(config_from_env, cls)
        else:
            ncls = cls()

        if "generation_config" in attrs:
            generation_config = attrs.pop("generation_config")
            if not LazyType(DictStrAny).isinstance(generation_config):
                raise RuntimeError(f"Expected a dictionary, but got {type(generation_config)}")
        else:
            generation_config = {k: v for k, v in attrs.items() if k in attr.fields_dict(ncls.generation_class)}

        attrs = {k: v for k, v in attrs.items() if k not in generation_config}
        ncls.generation_config = attr.evolve(ncls.generation_config, **generation_config)
        return attr.evolve(ncls, **attrs)

    def model_validate_click(self, **attrs: t.Any) -> tuple[LLMConfig, dict[str, t.Any]]:
        """Parse given click attributes into a LLMConfig and return the remaining click attributes."""
        llm_config_attrs: dict[str, t.Any] = {"generation_config": {}}
        key_to_remove: list[str] = []

        for k, v in attrs.items():
            if k.startswith(f"{self.__openllm_model_name__}_generation_"):
                llm_config_attrs["generation_config"][k[len(self.__openllm_model_name__ + "_generation_") :]] = v
                key_to_remove.append(k)
            elif k.startswith(f"{self.__openllm_model_name__}_"):
                llm_config_attrs[k[len(self.__openllm_model_name__ + "_") :]] = v
                key_to_remove.append(k)

        return self.model_construct_env(**llm_config_attrs), {k: v for k, v in attrs.items() if k not in key_to_remove}

    @t.overload
    def to_generation_config(self, return_as_dict: t.Literal[True] = ...) -> dict[str, t.Any]:
        ...

    @t.overload
    def to_generation_config(self, return_as_dict: t.Literal[False] = ...) -> transformers.GenerationConfig:
        ...

    def to_generation_config(self, return_as_dict: bool = False) -> transformers.GenerationConfig | dict[str, t.Any]:
        config = transformers.GenerationConfig(**bentoml_cattr.unstructure(self.generation_config))
        return config.to_dict() if return_as_dict else config

    @classmethod
    @t.overload
    def to_click_options(
        cls, f: t.Callable[..., openllm.LLMConfig]
    ) -> F[P, ClickFunctionWrapper[..., openllm.LLMConfig]]:
        ...

    @classmethod
    @t.overload
    def to_click_options(cls, f: t.Callable[P, O_co]) -> F[P, ClickFunctionWrapper[P, O_co]]:
        ...

    @classmethod
    def to_click_options(cls, f: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        """
        Convert current model to click options. This can be used as a decorator for click commands.
        Note that the identifier for all LLMConfig will be prefixed with '<model_name>_*', and the generation config
        will be prefixed with '<model_name>_generation_*'.
        """
        for name, field in attr.fields_dict(cls.generation_class).items():
            ty = cls.__openllm_hints__.get(name)
            if t.get_origin(ty) is t.Union:
                # NOTE: Union type is currently not yet supported, we probably just need to use environment instead.
                continue
            f = attrs_to_options(name, field, cls.__openllm_model_name__, typ=ty, suffix_generation=True)(f)
        f = optgroup.group(f"{cls.generation_class.__name__} generation options")(f)

        if len(cls.__openllm_attrs__) == 0:
            # NOTE: in this case, the function is already a ClickFunctionWrapper
            # hence the casting
            return f

        for name, field in attr.fields_dict(cls).items():
            ty = cls.__openllm_hints__.get(name)
            if t.get_origin(ty) is t.Union:
                # NOTE: Union type is currently not yet supported, we probably just need to use environment instead.
                continue
            f = attrs_to_options(name, field, cls.__openllm_model_name__, typ=ty)(f)

        return optgroup.group(f"{cls.__name__} options")(f)


bentoml_cattr.register_unstructure_hook_factory(
    lambda cls: lenient_issubclass(cls, LLMConfig),
    lambda cls: make_dict_unstructure_fn(cls, bentoml_cattr, _cattrs_omit_if_default=False),
)


def structure_llm_config(data: dict[str, t.Any], cls: type[LLMConfig]) -> LLMConfig:
    """
    Structure a dictionary to a LLMConfig object.

    Essentially, if the given dictionary contains a 'generation_config' key, then we will
    use it for LLMConfig.generation_config

    Otherwise, we will filter out all keys are first in LLMConfig, parse it in, then
    parse the remaining keys into LLMConfig.generation_config
    """
    if not LazyType(DictStrAny).isinstance(data):
        raise RuntimeError(f"Expected a dictionary, but got {type(data)}")

    cls_attrs = {k: v for k, v in data.items() if k in cls.__openllm_attrs__}
    generation_cls_fields = attr.fields_dict(cls.generation_class)
    if "generation_config" in data:
        generation_config = data.pop("generation_config")
        if not LazyType(DictStrAny).isinstance(generation_config):
            raise RuntimeError(f"Expected a dictionary, but got {type(generation_config)}")
        config_merger.merge(generation_config, {k: v for k, v in data.items() if k in generation_cls_fields})
    else:
        generation_config = {k: v for k, v in data.items() if k in generation_cls_fields}
    not_extras = list(cls_attrs) + list(generation_config)
    # The rest should be passed to extras
    data = {k: v for k, v in data.items() if k not in not_extras}

    return cls(generation_config=generation_config, __openllm_extras__=data, **cls_attrs)


bentoml_cattr.register_structure_hook_func(lambda cls: lenient_issubclass(cls, LLMConfig), structure_llm_config)
