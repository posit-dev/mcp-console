#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["joblib"]
# ///

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, joblib_processes, requires
from support.assertions import (
    last_result_text,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@requires(POSIX)
@requires(joblib_processes())
@executions(DIRECT, SANDBOXED)
def test_runs_sklearn_parallel_search(binary: Path, execution: Execution) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    # Exercise n_jobs=-1 without multiplying CI's own transcript concurrency.
    environment["LOKY_MAX_CPU_COUNT"] = "2"
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.send(python="import sys")
        assert last_tool_text(client) == "[done]"
        wait_for_evaluation_output(
            client,
            "[resolved PyPI distribution 'scikit-learn' for Python import 'sklearn']\n"
            "parallel search matches serial\n",
            "parallel grid search",
            completion_timeout_seconds=client.response_timeout,
            timeout_ms=0,
            # fmt: python
            python=code("""
                from sklearn.dummy import DummyClassifier
                from sklearn.model_selection import GridSearchCV
                from sklearn.pipeline import make_pipeline
                from sklearn.preprocessing import StandardScaler

                from joblib import effective_n_jobs
                from tempfile import TemporaryDirectory
                import numpy as np
                import os

                assert effective_n_jobs(-1) == 2
                # Just exceed the 1 MiB memmapping threshold without costly training.
                X = np.tile(np.arange(16, dtype=np.float64)[:, None], (1, 8193))
                y = np.arange(16) % 2
                assert X.nbytes > 1024**2
                model = make_pipeline(StandardScaler(), DummyClassifier(strategy="constant"))
                parameters = {"dummyclassifier__constant": [0, 1]}
                folds = [(np.arange(8, 16), np.arange(8)), (np.arange(8), np.arange(8, 16))]


                with TemporaryDirectory() as gates:
                    for constant in parameters["dummyclassifier__constant"]:
                        os.mkfifo(os.path.join(gates, f"workers-{constant}"))

                    def worker_pid(estimator, features, labels) -> int:
                        # Pair each candidate's folds so both process workers must run.
                        constant = estimator.named_steps["dummyclassifier"].constant
                        gate = os.path.join(gates, f"workers-{constant}")
                        if features[0, 0] == 0:
                            with open(gate, "wb") as pipe:
                                pipe.write(b"1")
                        else:
                            with open(gate, "rb") as pipe:
                                assert pipe.read(1) == b"1"
                        return os.getpid()

                    parallel = GridSearchCV(
                        model,
                        parameters,
                        cv=folds,
                        n_jobs=-1,
                        error_score="raise",
                        scoring={"accuracy": "accuracy", "worker": worker_pid},
                        refit="accuracy",
                    ).fit(X, y)
                worker_pids = set(parallel.cv_results_["split0_test_worker"]) | set(
                    parallel.cv_results_["split1_test_worker"]
                )
                assert len(worker_pids) == 2 and os.getpid() not in worker_pids
                serial = GridSearchCV(model, parameters, cv=folds, n_jobs=1, error_score="raise").fit(
                    X, y
                )
                np.testing.assert_array_equal(
                    parallel.cv_results_["mean_test_accuracy"],
                    serial.cv_results_["mean_test_score"],
                )
                np.testing.assert_array_equal(parallel.predict(X), serial.predict(X))
                print("parallel search matches serial")
                """),
        )
        wait_for_evaluation_output(
            client,
            "parallel cross-validation and importance match serial\n",
            "parallel cross-validation and importance",
            completion_timeout_seconds=client.response_timeout,
            timeout_ms=0,
            # fmt: python
            python=code("""
                from sklearn.ensemble import RandomForestClassifier
                from sklearn.inspection import permutation_importance
                from sklearn.model_selection import cross_val_score

                # Two trees and two features give each parallel API two tiny tasks.
                small_X = X[:, :2].copy()
                forest = RandomForestClassifier(
                    n_estimators=2, max_depth=1, random_state=42, n_jobs=-1
                ).fit(small_X, y)
                parallel_scores = cross_val_score(
                    forest, small_X, y, cv=2, n_jobs=-1, error_score="raise"
                )
                serial_scores = cross_val_score(forest, small_X, y, cv=2, n_jobs=1, error_score="raise")
                np.testing.assert_array_equal(parallel_scores, serial_scores)
                parallel_importance = permutation_importance(
                    forest, small_X, y, n_repeats=1, random_state=42, n_jobs=-1
                )
                serial_importance = permutation_importance(
                    forest, small_X, y, n_repeats=1, random_state=42, n_jobs=1
                )
                np.testing.assert_array_equal(
                    parallel_importance.importances, serial_importance.importances
                )
                print("parallel cross-validation and importance match serial")
                """),
        )
        # Direct workers do not own descendant retirement. Close the reusable
        # pool explicitly so that this execution mode leaves no idle children.
        wait_for_evaluation_output(
            client,
            "[done]",
            "process pool shutdown",
            completion_timeout_seconds=client.response_timeout,
            timeout_ms=0,
            # fmt: python
            python=code("""
                from joblib.externals.loky import get_reusable_executor

                get_reusable_executor().shutdown(wait=True)
                """),
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend(binary: Path, execution: Execution) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.expect(
        "[2, 1, 0, 1, 2]\n",
        python=python,
        requirements={"python": ["joblib"]},
    )
    client.expect(
        python="from joblib.externals.loky import get_reusable_executor\nget_reusable_executor().shutdown(wait=True)"
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()
    client.send(python="import sys")
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.expect("[2, 1, 0, 1, 2]\n", python=python)
    client.expect(
        python="from joblib.externals.loky import get_reusable_executor\nget_reusable_executor().shutdown(wait=True)"
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_spawn_process_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import multiprocessing.spawn
        import sys

        initial_executable = sys.executable
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        import multiprocessing
        import sys

        import joblib

        context = multiprocessing.get_context("spawn")
        with context.Pool(1) as pool:
            child_executable = pool.apply(
                eval,
                ("__import__('sys').executable",),
            )

        (
            initial_executable != sys.executable,
            child_executable == sys.executable,
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "(True, True)\n", repr(output)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
