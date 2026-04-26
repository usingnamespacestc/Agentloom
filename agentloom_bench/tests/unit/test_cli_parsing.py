"""Unit tests for ``--task-ids`` parser. Pure logic, no async."""
from __future__ import annotations

import pytest
import typer

from agentloom_bench.cli import _parse_task_ids


def test_parse_single():
    assert _parse_task_ids("0") == [0]


def test_parse_inclusive_range():
    assert _parse_task_ids("0-9") == list(range(10))


def test_parse_explicit_list():
    assert _parse_task_ids("0,1,5,7") == [0, 1, 5, 7]


def test_parse_combination():
    assert _parse_task_ids("0-2,5,7-9") == [0, 1, 2, 5, 7, 8, 9]


def test_parse_dedupes():
    assert _parse_task_ids("0,0,1-2,2,3") == [0, 1, 2, 3]


def test_parse_whitespace_tolerant():
    assert _parse_task_ids(" 0 - 2 , 5 , 7 - 9 ") == [0, 1, 2, 5, 7, 8, 9]


def test_parse_empty_raises():
    with pytest.raises(typer.BadParameter):
        _parse_task_ids("")


def test_parse_reversed_range_raises():
    with pytest.raises(typer.BadParameter):
        _parse_task_ids("9-0")


def test_parse_garbage_raises_value_error():
    # Non-numeric tokens propagate as ValueError from int() — caller
    # decides to re-raise as BadParameter or surface the error.
    with pytest.raises(ValueError):
        _parse_task_ids("a-b")
