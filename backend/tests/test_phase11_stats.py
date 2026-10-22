"""Malformed usage after a completed stream cannot cause repeat generation."""
import pytest
from pebble.stats import usage_metrics


@pytest.mark.parametrize('value',[True,-1,1.5,'3',float('inf'),10**400])
def test_invalid_cache_component_cannot_make_an_input_total(value):
    result=usage_metrics({'input_tokens':100,'output_tokens':4,'cache_read_input_tokens':value},{},0,2)
    assert result['input_tokens'] is None and result['cached_tokens'] is None
    assert result['output_tokens']==4


def test_valid_cache_components_are_counted_once():
    result=usage_metrics({'input_tokens':100,'output_tokens':4,'cache_read_input_tokens':20,'cache_creation_input_tokens':5},{},0,2)
    assert result['input_tokens']==125 and result['cached_tokens']==20 and result['cache_write_tokens']==5


def test_cache_above_reported_full_prompt_stays_unknown():
    result=usage_metrics({'prompt_tokens':100,'completion_tokens':4,'prompt_tokens_details':{'cached_tokens':101}},{},0,2)
    assert result['input_tokens']==100 and result['cached_tokens'] is None


@pytest.mark.parametrize('first,end',[(None,2),('bad',2),(0,'bad'),(float('inf'),2),(0,float('nan')),(3,2),(0,10**400)])
def test_bad_timing_does_not_discard_valid_counts(first,end):
    result=usage_metrics({'prompt_tokens':100,'completion_tokens':4},{},first,end)
    assert result['input_tokens']==100 and result['output_tokens']==4 and result['tokens_per_second'] is None


def test_huge_reported_rate_cannot_fail_a_completed_generation():
    result=usage_metrics({'prompt_tokens':100,'completion_tokens':4},{'predicted_per_second':10**400},0,2)
    assert result['input_tokens']==100 and result['output_tokens']==4 and result['tokens_per_second'] is None
