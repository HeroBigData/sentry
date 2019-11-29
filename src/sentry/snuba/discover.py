from __future__ import absolute_import

import six

from collections import namedtuple
from copy import deepcopy
from datetime import datetime

from sentry.api.event_search import (
    TAG_KEY_RE,
    get_filter,
    resolve_field_list,
    find_reference_event,
    InvalidSearchQuery,
)
from sentry.snuba.events import get_columns_from_aliases
from sentry.utils.snuba import (
    Dataset,
    SnubaTSResult,
    DISCOVER_COLUMN_MAP,
    QUOTED_LITERAL_RE,
    get_function_index,
    raw_query,
    transform_results,
    zerofill,
)

ReferenceEvent = namedtuple("ReferenceEvent", ["organization", "slug", "fields"])


def create_reference_event_conditions(reference):
    """
    Create a list of conditions based on a Reference object.

    This is useful when you want to get results that match an exemplar
    event. A use case of this is generating pagination links for, or getting
    timeseries results of the records inside a single aggregated row.

    reference (Reference) The reference event to build conditions from.
    """
    columns = get_columns_from_aliases(reference.fields)
    event_data = find_reference_event(reference.organization, reference.slug, columns)

    conditions = []
    tags = {}
    if "tags.key" in event_data and "tags.value" in event_data:
        tags = dict(zip(event_data["tags.key"], event_data["tags.value"]))

    field_names = [resolve_column(col) for col in reference.fields]
    for (i, field) in enumerate(reference.fields):
        match = TAG_KEY_RE.match(field_names[i])
        if match:
            value = tags.get(match.group(1), None)
        else:
            value = event_data.get(field_names[i], None)
            # If the value is a sequence use the first element as snuba
            # doesn't support `=` or `IN` operations on fields like exception_frames.filename
            if isinstance(value, (list, set)) and value:
                value = value.pop()
        if value:
            conditions.append([field, "=", value])

    return conditions


def resolve_column(col):
    """
    Resolve a public schema name to the discover dataset.
    unknown columns are converted into tags expressions.
    """
    if col is None:
        return col
    if col.startswith("tags[") or QUOTED_LITERAL_RE.match(col):
        return col
    return DISCOVER_COLUMN_MAP.get(col, u"tags[{}]".format(col))


def resolve_condition(cond):
    """
    When conditions have been parsed by the api.event_search module
    we can end up with conditions that are not valid on the current dataset
    due to how ap.event_search checks for valid field names without
    being aware of the dataset.

    We have the dataset context here, so we need to re-scope conditions to the
    current dataset.
    """
    index = get_function_index(cond)
    if index is not None:
        # IN conditions are detected as a function but aren't really.
        if cond[index] == "IN":
            cond[0] = resolve_column(cond[0])
            return cond

        func_args = cond[index + 1]
        for (i, arg) in enumerate(func_args):
            # Nested function
            if isinstance(arg, (list, tuple)):
                func_args[i] = resolve_condition(arg)
            else:
                func_args[i] = resolve_column(arg)
        cond[index + 1] = func_args
        return cond

    # No function name found
    if isinstance(cond, (list, tuple)) and len(cond):
        # Condition is [col, operator, value]
        if isinstance(cond[0], six.string_types) and len(cond) == 3:
            cond[0] = resolve_column(cond[0])
            return cond
        if isinstance(cond[0], (list, tuple)):
            if get_function_index(cond[0]) is not None:
                cond[0] = resolve_condition(cond[0])
                return cond
            else:
                # Nested conditions
                return [resolve_condition(item) for item in cond]
    raise ValueError("Unexpected condition format %s" % cond)


def resolve_discover_aliases(snuba_args):
    """
    Resolve the public schema aliases to the discover dataset.

    Returns a copy of the input structure, and includes a
    `translated_columns` key containing the selected fields that need to
    be renamed in the result set.
    """
    resolved = deepcopy(snuba_args)
    translated_columns = {}
    derived_columns = set()

    selected_columns = resolved.get("selected_columns")
    if selected_columns:
        for (idx, col) in enumerate(selected_columns):
            if isinstance(col, (list, tuple)):
                raise ValueError("discover selected_columns should only be str. got %s" % col)
            name = resolve_column(col)
            selected_columns[idx] = name
            translated_columns[name] = col
        resolved["selected_columns"] = selected_columns

    groupby = resolved.get("groupby")
    if groupby:
        for (idx, col) in enumerate(groupby):
            name = col
            if col not in derived_columns:
                name = resolve_column(col)
            groupby[idx] = name
        resolved["groupby"] = groupby

    aggregations = resolved.get("aggregations")
    for aggregation in aggregations or []:
        derived_columns.add(aggregation[2])
        if isinstance(aggregation[1], six.string_types):
            aggregation[1] = resolve_column(aggregation[1])
        elif isinstance(aggregation[1], (set, tuple, list)):
            aggregation[1] = [resolve_column(col) for col in aggregation[1]]
    resolved["aggregations"] = aggregations

    conditions = resolved.get("conditions")
    if conditions:
        for (i, condition) in enumerate(conditions):
            replacement = resolve_condition(condition)
            conditions[i] = replacement
        resolved["conditions"] = list(filter(None, conditions))

    # TODO add support for extracting having conditions.

    orderby = resolved.get("orderby")
    if orderby:
        orderby = orderby if isinstance(orderby, (list, tuple)) else [orderby]
        resolved_orderby = []

        for field_with_order in orderby:
            field = field_with_order.lstrip("-")
            resolved_orderby.append(
                u"{}{}".format(
                    "-" if field_with_order.startswith("-") else "",
                    field if field in derived_columns else resolve_column(field),
                )
            )
        resolved["orderby"] = resolved_orderby
    return resolved, translated_columns


def query(selected_columns, query, params, orderby=None, referrer=None, auto_fields=False):
    """
    High-level API for doing arbitrary user queries against events.

    This function operates on the Discover public event schema and
    virtual fields/aggregate functions for selected columns and
    conditions are supported through this function.

    The resulting list will have all internal field names mapped
    back into their public schema names.

    selected_columns (Sequence[str]) List of public aliases to fetch.
    query (str) Filter query string to create conditions from.
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment
    orderby (None|str|Sequence[str]) The field to order results by.
    referrer (str|None) A referrer string to help locate the origin of this query.
    auto_fields (bool) Set to true to have project + eventid fields automatically added.
    """
    snuba_filter = get_filter(query, params)

    # TODO(mark) Refactor the need for this translation shim once all of
    # discover is using this module. Remember to update all the functions
    # in this module.
    snuba_args = {
        "start": snuba_filter.start,
        "end": snuba_filter.end,
        "conditions": snuba_filter.conditions,
        "filter_keys": snuba_filter.filter_keys,
        "orderby": orderby,
    }
    snuba_args.update(resolve_field_list(selected_columns, snuba_args, auto_fields=auto_fields))

    # Resolve the public aliases into the discover dataset names.
    snuba_args, translated_columns = resolve_discover_aliases(snuba_args)

    result = raw_query(
        start=snuba_args.get("start"),
        end=snuba_args.get("end"),
        groupby=snuba_args.get("groupby"),
        conditions=snuba_args.get("conditions"),
        aggregations=snuba_args.get("aggregations"),
        selected_columns=snuba_args.get("selected_columns"),
        filter_keys=snuba_args.get("filter_keys"),
        orderby=snuba_args.get("orderby"),
        dataset=Dataset.Discover,
        referrer=referrer,
    )

    return transform_results(result, translated_columns, snuba_args)


def timeseries_query(selected_columns, query, params, rollup, reference=None, referrer=None):
    """
    High-level API for doing arbitrary user timeseries queries against events.

    This function operates on the public event schema and
    virtual fields/aggregate functions for selected columns and
    conditions are supported through this function.

    This function is intended to only get timeseries based
    results and thus requires the `rollup` parameter.

    Returns a SnubaTSResult object that has been zerofilled in
    case of gaps.

    selected_columns (Sequence[str]) List of public aliases to fetch.
    query (str) Filter query string to create conditions from.
    params (Dict[str, str]) Filtering parameters with start, end, project_id, environment,
    rollup (int) The bucket width in seconds
    reference (Reference) A reference event object. Used to generate additional conditions
                          based on the provided reference.
    referrer (str|None) A referrer string to help locate the origin of this query.
    """
    snuba_filter = get_filter(query, params)

    # Use fallbacks for date filters as zerofill() depends on dates.
    snuba_args = {
        "start": snuba_filter.start or datetime.utcfromtimestamp(0),
        "end": snuba_filter.end or datetime.utcnow(),
        "conditions": snuba_filter.conditions,
        "filter_keys": snuba_filter.filter_keys,
    }
    snuba_args.update(resolve_field_list(selected_columns, snuba_args, auto_fields=False))

    if reference:
        ref_conditions = create_reference_event_conditions(reference)
        if ref_conditions:
            snuba_args["conditions"].extend(ref_conditions)

    # Resolve the public aliases into the discover dataset names.
    snuba_args, _ = resolve_discover_aliases(snuba_args)
    if not snuba_args["aggregations"]:
        raise InvalidSearchQuery("Cannot get timeseries result with no aggregation.")

    result = raw_query(
        aggregations=snuba_args.get("aggregations"),
        conditions=snuba_args.get("conditions"),
        filter_keys=snuba_args.get("filter_keys"),
        start=snuba_args.get("start"),
        end=snuba_args.get("end"),
        rollup=rollup,
        orderby="time",
        groupby=["time"],
        dataset=Dataset.Discover,
        limit=10000,
        referrer=referrer,
    )
    result = zerofill(result["data"], snuba_args["start"], snuba_args["end"], rollup, "time")

    return SnubaTSResult(result, snuba_filter.start, snuba_filter.end, rollup)


def get_pagination_ids(event, query, params):
    """
    High-level API for getting pagination data for an event + filter

    The provided event is used as a reference event to find events
    that are older and newer than the current one.
    """
    raise NotImplementedError