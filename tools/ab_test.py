#!/usr/bin/env python3
# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Script for running A/B-Tests

The script takes two git revisions and a pytest integration test. It utilizes
our integration test frameworks --binary-dir parameter to execute the given
test using binaries compiled from each revision, and captures the EMF logs
output. It the searches for list-valued properties/metrics in the EMF, and runs a
regression test comparing these lists for the two runs.

It performs the A/B-test as follows:
For each EMF log message output, look at the dimensions. The script assumes that
dimensions are unique across all log messages output from a single test run. In
each log message, then look for all properties that have lists assigned to them,
and collect them. For both runs of the test, the set of distinct dimensions
collected this way must be the same. Then, we match corresponding dimensions
between the two runs, performing statistical regression test across all the list-
valued properties collected.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

# Hack to be able to use our test framework code
sys.path.append(str(Path(__file__).parent.parent / "tests"))

# pylint:disable=wrong-import-position
from framework import utils
from framework.ab_test import check_regression, git_ab_test_with_binaries
from framework.properties import global_props
from host_tools.metrics import (
    emit_raw_emf,
    format_with_reduced_unit,
    get_metrics_logger,
)

# Performance tests that are known to be unstable and exhibit variances of up to 60% of the mean
IGNORED = [
    # Network throughput on m6a.metal
    {"instance": "m6a.metal", "performance_test": "test_network_tcp_throughput"},
    # Block throughput for 1 vcpu on m6g.metal/5.10
    {
        "performance_test": "test_block_performance",
        "instance": "m6g.metal",
        "host_kernel": "linux-5.10",
        "vcpus": "1",
    },
]


def is_ignored(dimensions) -> bool:
    """Checks whether the given dimensions match a entry in the IGNORED dictionary above"""
    for high_variance in IGNORED:
        matching = {key: dimensions[key] for key in high_variance}

        if matching == high_variance:
            return True

    return False


def extract_dimensions(emf):
    """Extracts the cloudwatch dimensions from an EMF log message"""
    if not emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"]:
        # Skipped tests emit a duration metric, but have no dimensions set
        return {}

    dimension_list = emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]
    return {key: emf[key] for key in emf if key in dimension_list}


def reemit_emf_and_get_data(log_entry: str, revision: str):
    """Parses the given EMF log entry, and reemits it, overwriting the attached "git_commit_id" field
    with the given revision

    Returns the entries dimensions and its list-valued properties/metrics, together with their units
    """
    emf = json.loads(log_entry)
    emf["git_commit_id"] = revision
    emit_raw_emf(emf)

    result = {
        key: (value, find_unit(emf, key))
        for key, value in emf.items()
        if isinstance(value, list)
    }

    return extract_dimensions(emf), result


def find_unit(emf: dict, metric: str):
    """Determines the unit of the given metric"""
    metrics = {
        y["Name"]: y["Unit"] for y in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    return metrics.get(metric, "None")


def load_data_series(revision: str):
    """Loads the data series relevant for A/B-testing from test_results/test-report.json
    into a dictionary mapping each message's cloudwatch dimensions to a dictionary of
    its list-valued properties/metrics.

    Also reemits all EMF logs."""
    # Dictionary mapping EMF dimensions to A/B-testable metrics/properties
    processed_emf = {}

    report = json.loads(Path("test_results/test-report.json").read_text("UTF-8"))
    for test in report["tests"]:
        for line in test["teardown"]["stdout"].splitlines():
            # Only look at EMF log messages. If we ever have other stdout that starts with braces,
            # we will need to rethink this heuristic.
            if line.startswith("{"):
                dimensions, result = reemit_emf_and_get_data(line, revision)

                if not dimensions:
                    continue

                dimension_set = frozenset(dimensions.items())

                if dimension_set not in processed_emf:
                    processed_emf[dimension_set] = result
                else:
                    # If there are many data points for a metric, they will be split across
                    # multiple EMF log messages. We need to reassemble :(
                    assert processed_emf[dimension_set].keys() == result.keys()

                    for metric, (values, unit) in processed_emf[dimension_set].items():
                        assert result[metric][1] == unit

                        values.extend(result[metric][0])

    return processed_emf


def collect_data(firecracker_binary: Path, jailer_binary: Path, test: str):
    """Executes the specified test using the provided firecracker binaries"""
    # Ensure the binaries are in the same directory. Will always be the case if used with git_ab_test_with_binaries
    assert jailer_binary.parent == firecracker_binary.parent

    binary_dir = firecracker_binary.parent
    revision = binary_dir.name

    print("Collecting samples")
    _, stdout, _ = utils.run_cmd(
        f"AWS_EMF_ENVIRONMENT=local AWS_EMF_NAMESPACE=local ./tools/test.sh --binary-dir=/firecracker/build/{revision} {test} -m ''"
    )
    print(stdout.strip())

    return load_data_series(revision)


def analyze_data(processed_emf_a, processed_emf_b, *, n_resamples: int = 9999):
    """
    Analyzes the A/B-test data produced by `collect_data`, by performing regression tests
    as described this script's doc-comment.

    Returns a mapping of dimensions and properties/metrics to the result of their regression test.
    """
    assert set(processed_emf_a.keys()) == set(
        processed_emf_b.keys()
    ), "A and B run produced incomparable data. This is a bug in the test!"

    results = {}

    metrics_logger = get_metrics_logger()

    for prop_name, prop_val in global_props.__dict__.items():
        metrics_logger.set_property(prop_name, prop_val)

    for dimension_set in processed_emf_a:
        metrics_a = processed_emf_a[dimension_set]
        metrics_b = processed_emf_b[dimension_set]

        assert set(metrics_a.keys()) == set(
            metrics_b.keys()
        ), "A and B run produced incomparable data. This is a bug in the test!"

        for metric, (values_a, unit) in metrics_a.items():
            print(
                f"Doing A/B-test for dimensions {dimension_set} and property {metric}"
            )
            result = check_regression(
                values_a, metrics_b[metric][0], n_resamples=n_resamples
            )

            metrics_logger.set_dimensions({"metric": metric, **dict(dimension_set)})
            metrics_logger.put_metric("p_value", float(result.pvalue), "None")
            metrics_logger.put_metric("mean_difference", float(result.statistic), unit)
            metrics_logger.set_property("data_a", values_a)
            metrics_logger.set_property("data_b", metrics_b[metric][0])
            metrics_logger.flush()

            results[dimension_set, metric] = (result, unit)

    return results


def ab_performance_test(
    a_revision, b_revision, test, p_thresh, strength_thresh, noise_threshold
):
    """Does an A/B-test of the specified test across the given revisions"""
    _, commit_list, _ = utils.run_cmd(
        f"git --no-pager log --oneline {a_revision}..{b_revision}"
    )
    print(
        f"Performance A/B-test across {a_revision}..{b_revision}. This includes the following commits:"
    )
    print(commit_list.strip())

    processed_emf_a, processed_emf_b, results = git_ab_test_with_binaries(
        lambda firecracker_binary, jailer_binary: collect_data(
            firecracker_binary, jailer_binary, test
        ),
        lambda ah, be: analyze_data(ah, be, n_resamples=int(100 / p_thresh)),
        a_revision=a_revision,
        b_revision=b_revision,
    )

    # We sort our A/B-Testing results keyed by metric here. The resulting lists of values
    # will be approximately normal distributed, and we will use this property as a means of error correction.
    # The idea behind this is that testing the same metric (say, restore_latency) across different scenarios (e.g.
    # different vcpu counts) will be related in some unknown way (meaning most scenarios will show a change in the same
    # direction). In particular, if one scenario yields a slight improvement and the next yields a
    # slight degradation, we take this as evidence towards both being mere noise that cancels out.
    #
    # Empirical evidence for this assumption is that
    #  1. Historically, a true performance change has never shown up in just a single test, it always showed up
    #     across most (if not all) tests for a specific metric.
    #  2. Analyzing data collected from historical runs shows that across different parameterizations of the same
    #     metric, the collected samples approximately follow mean / variance = const, with the constant independent
    #     of the parameterization.
    #
    # Mathematically, this has the following justification: By the central
    # limit theorem, the means of samples are (approximately) normal distributed. Denote by A
    # and B the distributions of the mean of samples from the 'A' and 'B'
    # tests respectively. Under our null hypothesis, the distributions of the
    # 'A' and 'B' samples are identical (although we dont know what the exact
    # distributions are), meaning so are A and B, say A ~ B ~ N(mu, sigma^2).
    # The difference of two normal distributions is also normal distributed,
    # with the means being subtracted and the variances being added.
    # Therefore, A - B ~ N(0, 2sigma^2). If we now normalize this distribution by mu (which
    # corresponds to considering the distribution of relative regressions instead), we get (A-B)/mu ~ N(0, c), with c
    # being the constant from point 2. above. This means that we can combine the relative means across
    # different parameterizations, and get a distributions whose expected
    # value is 0, provided our null hypothesis was true. It is exactly this distribution
    # for which we collect samples in the dictionary below. Therefore, a sanity check
    # on the average of the average of the performance changes for a single metric
    # is a good candidates for a sanity check against false-positives.
    #
    # Note that with this approach, for performance changes to "cancel out", we would need essentially a perfect split
    # between scenarios that improve performance and scenarios that degrade performance, something we have not
    # ever observed to actually happen.
    relative_changes_by_metric = {}

    failures = []
    for (dimension_set, metric), (result, unit) in results.items():
        if is_ignored(dict(dimension_set)):
            continue

        values_a = processed_emf_a[dimension_set][metric][0]
        baseline_mean = statistics.mean(values_a)

        if metric not in relative_changes_by_metric:
            relative_changes_by_metric[metric] = []
        relative_changes_by_metric[metric].append(result.statistic / baseline_mean)

        if (
            result.pvalue < p_thresh
            and abs(result.statistic) > baseline_mean * strength_thresh
        ):
            failures.append((dimension_set, metric, result, unit))

    failure_report = "\n".join(
        f"\033[0;32m[Firecracker A/B-Test Runner]\033[0m A/B-testing shows a change of "
        f"\033[0;31m\033[1m{format_with_reduced_unit(result.statistic, unit)}\033[0m "
        f"(from {format_with_reduced_unit(statistics.mean(processed_emf_a[dimension_set][metric][0]), unit)} "
        f"to {format_with_reduced_unit(statistics.mean(processed_emf_b[dimension_set][metric][0]), unit)}) "
        f"for metric \033[1m{metric}\033[0m with \033[0;31m\033[1mp={result.pvalue}\033[0m. "
        f"This means that observing a change of this magnitude or worse, assuming that performance "
        f"characteristics did not change across the tested commits, has a probability of {result.pvalue:.2%}. "
        f"Tested Dimensions:\n{json.dumps(dict(dimension_set), indent=2)}"
        for (dimension_set, metric, result, unit) in failures
        # Sanity check as described above
        if abs(statistics.mean(relative_changes_by_metric[metric])) > noise_threshold
    )
    assert not failure_report, "\n" + failure_report
    print("No regressions detected!")


def canonicalize_revision(revision):
    """Canonicalizes the given revision to a 40 digit hex SHA"""
    return utils.run_cmd(f"git rev-parse {revision}").stdout.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Executes Firecracker's A/B testsuite across the specified commits"
    )
    parser.add_argument(
        "a_revision",
        help="The baseline revision compared to which we want to avoid regressing",
    )
    parser.add_argument(
        "b_revision",
        help="The revision whose performance we want to compare against the results from a_revision",
    )
    parser.add_argument("--test", help="The test to run", required=True)
    parser.add_argument(
        "--significance",
        help="The p-value threshold that needs to be crossed for a test result to be considered significant",
        type=float,
        default=0.01,
    )
    parser.add_argument(
        "--relative-strength",
        help="The minimal delta required before a regression will be considered valid",
        type=float,
        default=0.0,
    )
    parser.add_argument("--noise-threshold", type=float, default=0.05)
    args = parser.parse_args()

    ab_performance_test(
        # These will show up in Cloudwatch, so canonicalize to long commit SHAs
        canonicalize_revision(args.a_revision),
        canonicalize_revision(args.b_revision),
        args.test,
        args.significance,
        args.relative_strength,
        args.noise_threshold,
    )
