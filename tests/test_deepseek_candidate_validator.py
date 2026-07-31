# поток: gab
"""Offline tests for tools/deepseek_candidate_validator.py.

No test in this module performs a network or DeepSeek API request.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.deepseek_candidate_validator import (
    ALLOWED_RESULTS,
    RESULT_FILES,
    SourceDocument,
    SourceUnavailableError,
    ValidationPipeline,
    load_candidates,
    validate_model_result,
)


def candidate(code: str, status: str = "READY_FOR_VALIDATION") -> dict[str, str]:
    return {
        "vendorCode": code,
        "_structural_check": status,
        "manufacturer": "HP",
        "model": "CF123A",
        "oem_code": "CF123A",
        "title": "HP CF123A Black",
        "original_dimensions": "100 x 50 x 40",
        "original_unit": "mm",
        "dimensions_cm": "[10,5,4]",
        "dimension_type": "package",
        "url": f"https://example.test/{code}",
        "source_url": "",
        "source_1_url": "",
        "source_2_url": "",
        "confirming_fragment": "CF123A Package dimensions 100 x 50 x 40 mm",
    }


def confirmed_payload() -> dict:
    return {
        "status": "CONFIRMED",
        "exact_model_found": "CF123A",
        "manufacturer_found": "HP",
        "color_suffix_check": "black suffix exact",
        "originality": "OEM_ORIGINAL",
        "package_quantity": "SINGLE",
        "dimension_scope": "PACKAGE",
        "source_dimensions_original": "100 x 50 x 40 mm",
        "source_unit": "mm",
        "source_dimensions_cm": [10, 5, 4],
        "model_match": True,
        "manufacturer_match": True,
        "color_suffix_match": True,
        "values_match": True,
        "conversion_match": True,
        "evidence_quote": "CF123A Package dimensions 100 x 50 x 40 mm",
        "reason": "All independent checks passed.",
    }


class FakeFetcher:
    def __init__(self, unavailable: set[str] | None = None):
        self.calls: list[str] = []
        self.unavailable = unavailable or set()

    def fetch(self, urls):
        url = list(urls)[0]
        self.calls.append(url)
        if url.rsplit("/", 1)[-1] in self.unavailable:
            raise SourceUnavailableError("mock unavailable")
        return SourceDocument(
            url=url,
            title="Mock HP CF123A",
            text="HP CF123A original. One cartridge. Package dimensions 100 x 50 x 40 mm.",
            content_type="text/html",
            status_code=200,
        )


class FakeClient:
    def __init__(self, statuses: dict[str, str] | None = None):
        self.calls: list[str] = []
        self.statuses = statuses or {}

    def validate(self, row, source, *, max_source_chars):
        code = row["vendorCode"]
        self.calls.append(code)
        payload = confirmed_payload()
        payload["status"] = self.statuses.get(code, "CONFIRMED")
        payload = validate_model_result(payload)
        return payload, {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        }


class FailingClient:
    def __init__(self):
        self.calls: list[str] = []

    def validate(self, row, source, *, max_source_chars):
        self.calls.append(row["vendorCode"])
        raise RuntimeError("mock API outage")


class DeepSeekCandidateValidatorTests(unittest.TestCase):
    def test_load_candidates_filters_ready_and_requires_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.csv"
            rows = [
                candidate("1"),
                candidate("2"),
                candidate("3", "REJECTED"),
            ]
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(rows[0]), delimiter=";"
                )
                writer.writeheader()
                writer.writerows(rows)
            loaded = load_candidates(path, expected_ready_count=2)
            self.assertEqual(["0001", "0002"], [row["vendorCode"] for row in loaded])

    def test_load_candidates_can_include_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.csv"
            rows = [candidate("1"), candidate("2", "INCOMPLETE")]
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(rows[0]), delimiter=";"
                )
                writer.writeheader()
                writer.writerows(rows)
            loaded = load_candidates(
                path, expected_ready_count=2, include_incomplete=True
            )
            self.assertEqual(["0001", "0002"], [row["vendorCode"] for row in loaded])

    def test_incomplete_confirmed_is_fail_closed(self):
        payload = confirmed_payload()
        payload["package_quantity"] = "MULTIPACK"
        result = validate_model_result(payload)
        self.assertEqual("NEEDS_MANUAL_REVIEW", result["status"])
        self.assertIn("fail-closed", result["reason"])

    def test_checkpoint_resume_and_outputs_are_offline(self):
        rows = [candidate("0001"), candidate("0002"), candidate("0003")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first_fetcher = FakeFetcher()
            first_client = FakeClient()
            first = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=first_fetcher,
                client=first_client,
                workers=2,
            )
            completed = first.run(limit=2)
            self.assertEqual(2, len(completed))
            self.assertEqual(2, len(first_client.calls))
            checkpoint_lines = [
                line
                for line in (output / "checkpoint.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(2, len(checkpoint_lines))

            second_fetcher = FakeFetcher()
            second_client = FakeClient()
            second = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=second_fetcher,
                client=second_client,
                workers=1,
            )
            completed = second.run()
            self.assertEqual(3, len(completed))
            self.assertEqual(["0003"], second_client.calls)
            checkpoint_lines = [
                line
                for line in (output / "checkpoint.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(3, len(checkpoint_lines))
            for filename in [
                *RESULT_FILES.values(),
                "validation_results.xlsx",
                "report.md",
                "SHA256SUMS.txt",
            ]:
                self.assertTrue((output / filename).exists(), filename)

    def test_unavailable_source_does_not_call_model(self):
        rows = [candidate("0001")]
        with tempfile.TemporaryDirectory() as directory:
            fetcher = FakeFetcher(unavailable={"0001"})
            client = FakeClient()
            pipeline = ValidationPipeline(
                candidates=rows,
                output_dir=Path(directory),
                fetcher=fetcher,
                client=client,
                workers=1,
            )
            completed = pipeline.run()
            self.assertEqual(
                "SOURCE_UNAVAILABLE", completed["0001"]["validation_status"]
            )
            self.assertEqual([], client.calls)
            self.assertEqual(0, completed["0001"]["total_tokens"])

    def test_api_failure_is_not_checkpointed_and_remains_resumable(self):
        rows = [candidate("0001")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            pipeline = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=FakeFetcher(),
                client=FailingClient(),
                workers=1,
            )
            with self.assertRaisesRegex(RuntimeError, "remain unfinished"):
                pipeline.run()
            checkpoint = output / "checkpoint.jsonl"
            self.assertFalse(checkpoint.exists())

            retry_client = FakeClient()
            retry = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=FakeFetcher(),
                client=retry_client,
                workers=1,
            )
            completed = retry.run()
            self.assertEqual(["0001"], retry_client.calls)
            self.assertEqual("CONFIRMED", completed["0001"]["validation_status"])

    def test_status_files_partition_completed_rows(self):
        rows = [candidate("0001"), candidate("0002"), candidate("0003")]
        statuses = {
            "0001": "CONFIRMED",
            "0002": "DOWNGRADED_REJECTED",
            "0003": "NEEDS_MANUAL_REVIEW",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            pipeline = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=FakeFetcher(),
                client=FakeClient(statuses),
                workers=3,
            )
            pipeline.run()
            total = 0
            seen: set[str] = set()
            for filename in RESULT_FILES.values():
                with (output / filename).open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    result_rows = list(csv.DictReader(handle, delimiter=";"))
                total += len(result_rows)
                for row in result_rows:
                    self.assertNotIn(row["vendorCode"], seen)
                    seen.add(row["vendorCode"])
            self.assertEqual(3, total)
            self.assertEqual({"0001", "0002", "0003"}, seen)

    def test_checkpoint_rejects_unknown_status(self):
        rows = [candidate("0001")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            output.joinpath("checkpoint.jsonl").write_text(
                json.dumps(
                    {
                        "vendorCode": "0001",
                        "validation_status": "CANDIDATE",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pipeline = ValidationPipeline(
                candidates=rows,
                output_dir=output,
                fetcher=Mock(),
                client=Mock(),
                workers=1,
            )
            with self.assertRaises(ValueError):
                pipeline.load_checkpoint()

    def test_allowed_results_are_exact(self):
        self.assertEqual(
            {
                "CONFIRMED",
                "DOWNGRADED_REJECTED",
                "NEEDS_MANUAL_REVIEW",
                "SOURCE_UNAVAILABLE",
            },
            ALLOWED_RESULTS,
        )


if __name__ == "__main__":
    unittest.main()
