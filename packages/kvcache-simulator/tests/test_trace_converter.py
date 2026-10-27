from __future__ import annotations

import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "convert_semianalysis_trace.py"
SPEC = importlib.util.spec_from_file_location("trace_converter", SCRIPT)
converter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = converter
SPEC.loader.exec_module(converter)


def request(t=0, duration=1, *, hashes=None, model="model-a", **extra):
    return {
        "t": t, "api_time": duration, "in": 65, "out": 3,
        "hash_ids": [0, 1] if hashes is None else hashes,
        "model": model, "type": "s", **extra,
    }


def conversation(identifier, *requests):
    return {"id": identifier, "models": ["model-a"], "block_size": 64,
            "hash_id_scope": "local", "requests": list(requests)}


def group(identifier, *requests):
    return {"type": "subagent", "agent_id": identifier, "t": 99,
            "duration_ms": 999999, "requests": list(requests)}


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.jsonl"
        self.output = self.root / "output.jsonl"
        self.spools = self.root / "spools"
        self.spools.mkdir()

    def write_source(self, *conversations):
        self.source.write_text("\n".join(json.dumps(c) for c in conversations) + "\n", encoding="utf-8")

    def convert(self, *conversations, concurrency=1):
        self.write_source(*conversations)
        stats = converter.convert(self.source, self.output, concurrency=concurrency, temp_dir=self.spools)
        self.assertEqual(list(self.spools.iterdir()), [])
        with self.output.open() as handle:
            return [json.loads(line) for line in handle], stats

    def test_fields_and_root_conversation_id_on_every_nested_request(self):
        rows, stats = self.convert(conversation(
            "original-root-id", request(10, ttft=0.5, think_time=2),
            group("child", request(2), group("grandchild", request(4))), request(5),
        ))
        self.assertEqual([r["timestamp"] for r in rows], [2, 4, 5, 10])
        self.assertEqual([r["request_index"] for r in rows], [1, 2, 3, 0])
        self.assertEqual([r["agent_id"] for r in rows], ["child", "grandchild", None, None])
        self.assertTrue(all(r["conversation_id"] == "original-root-id" for r in rows))
        self.assertTrue(all(r["conversation_index"] == 0 for r in rows))
        for row in rows:
            self.assertEqual(row["input_length"], 65)
            self.assertEqual(row["output_length"], 3)
            self.assertEqual(row["block_size"], 64)
            self.assertEqual(row["completion_timestamp"], row["timestamp"] + row["api_time"])
            self.assertEqual(row["relative_timestamp"], row["timestamp"])
            self.assertEqual(row["hash_ids"], rows[0]["hash_ids"])
        self.assertEqual(rows[-1]["ttft"], 0.5)
        self.assertEqual(rows[-1]["think_time"], 2)
        self.assertEqual((stats.conversation_count, stats.request_count, stats.unique_hash_ids), (1, 4, 2))

    def test_default_slot_includes_execution_and_thinking_gaps(self):
        rows, stats = self.convert(
            conversation("A", request(0, 5), request(10, 2, think_time=5)),
            conversation("B", request(0, 3)),
        )
        self.assertEqual([r["timestamp"] for r in rows], [0, 10, 12])
        self.assertEqual(rows[-1]["conversation_start_timestamp"], 12)
        self.assertEqual(rows[-1]["relative_timestamp"], 0)
        self.assertEqual(stats.replay_duration, 15)

    def test_rolling_slots_replace_earliest_finished_conversation(self):
        rows, stats = self.convert(
            conversation("A", request(0, 10)),
            conversation("B", request(0, 2)),
            conversation("C", request(0, 1), request(2, 3)),
            conversation("D", request(0, 1)),
            concurrency=2,
        )
        self.assertEqual([(r["conversation_id"], r["timestamp"]) for r in rows],
                         [("A", 0), ("B", 0), ("C", 2), ("C", 4), ("D", 7)])
        self.assertEqual(stats.replay_duration, 10)

    def test_completion_waits_for_long_earlier_and_subagent_requests(self):
        rows, _ = self.convert(
            conversation("A", request(0, 30), group("child", request(2, 40)), request(3, 1)),
            conversation("B", request()),
        )
        self.assertEqual(rows[-1]["timestamp"], 42)

    def test_zero_duration_ties_and_input_exhaustion(self):
        sessions = [conversation("A", request(0, 0), request(0, 0)),
                    conversation("B", request(0, 0)), conversation("C", request(0, 0))]
        for concurrency in (1, 2, 10):
            with self.subTest(concurrency=concurrency):
                rows, stats = self.convert(*sessions, concurrency=concurrency)
                self.assertEqual([r["conversation_id"] for r in rows], ["A", "A", "B", "C"])
                self.assertEqual(stats.replay_duration, 0)

    def test_hash_identity_is_local_to_conversation_occurrence_and_model(self):
        rows, _ = self.convert(
            conversation("same-id", request(), group("child", request(1)),
                         request(2, model="model-b")),
            conversation("same-id", request()),
        )
        self.assertEqual(rows[0]["hash_ids"], rows[1]["hash_ids"])
        self.assertTrue(set(rows[0]["hash_ids"]).isdisjoint(rows[2]["hash_ids"]))
        self.assertTrue(set(rows[0]["hash_ids"]).isdisjoint(rows[3]["hash_ids"]))
        self.assertTrue(all(type(h) is int for row in rows for h in row["hash_ids"]))

    def test_deterministic_output_and_hash_mapping_across_concurrency(self):
        sessions = [conversation("A", request(), request(4)), conversation("B", request())]
        first, _ = self.convert(*sessions, concurrency=2)
        serialized = self.output.read_bytes()
        self.convert(*sessions, concurrency=2)
        self.assertEqual(self.output.read_bytes(), serialized)
        second, _ = self.convert(*sessions, concurrency=1)
        ids = lambda rows: {(r["conversation_index"], r["request_index"]): r["hash_ids"] for r in rows}
        self.assertEqual(ids(first), ids(second))

    def test_gzip_io(self):
        self.source = self.root / "source.jsonl.gz"
        self.output = self.root / "output.jsonl.gz"
        with gzip.open(self.source, "wt") as handle:
            handle.write(json.dumps(conversation("gzip-id", request())) + "\n")
        converter.convert(self.source, self.output, temp_dir=self.spools)
        with gzip.open(self.output, "rt") as handle:
            self.assertEqual(json.loads(handle.readline())["conversation_id"], "gzip-id")

    def test_cli_streams_json_to_stdout_and_summary_to_stderr(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", "-", "--output", "-", "--temp-dir", str(self.spools)],
            input="\n" + json.dumps(conversation("stdin-id", request())) + "\n",
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["conversation_id"], "stdin-id")
        self.assertIn("1 conversations, 1 requests", result.stderr)
        self.assertEqual(list(self.spools.iterdir()), [])

    def test_invalid_input_reports_context_and_preserves_existing_output(self):
        malformed = conversation("B", group("child", request(api_time=-1)))
        self.write_source(conversation("A", request()), malformed)
        self.output.write_text("previous result")
        with self.assertRaisesRegex(ValueError, r"line 2: requests\[0\].requests\[0\].api_time"):
            converter.convert(self.source, self.output, temp_dir=self.spools)
        self.assertEqual(self.output.read_text(), "previous result")
        self.assertEqual(list(self.spools.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["output.jsonl", "source.jsonl", "spools"])

    def test_required_fields_and_consistent_block_sizes(self):
        for field, value in (("id", None), ("block_size", 0), ("hash_id_scope", "global"), ("requests", [])):
            with self.subTest(field=field):
                invalid = conversation("invalid", request())
                invalid[field] = value
                self.write_source(invalid)
                with self.assertRaises(ValueError):
                    converter.convert(self.source, self.output, temp_dir=self.spools)
        for field, value in (("t", float("nan")), ("api_time", float("inf")), ("in", 0),
                             ("out", -1), ("model", None), ("hash_ids", [True])):
            with self.subTest(field=field):
                invalid = request()
                invalid[field] = value
                self.write_source(conversation("invalid", invalid))
                with self.assertRaises(ValueError):
                    converter.convert(self.source, self.output, temp_dir=self.spools)
        invalid = conversation("B", request())
        invalid["block_size"] = 512
        self.write_source(conversation("A", request()), invalid)
        with self.assertRaisesRegex(ValueError, "differs from earlier block_size"):
            converter.convert(self.source, self.output, temp_dir=self.spools)

    def test_empty_malformed_input_and_invalid_concurrency(self):
        for raw in ("\n", "{invalid json}\n", "[]\n"):
            self.source.write_text(raw)
            with self.assertRaises(ValueError):
                converter.convert(self.source, self.output, temp_dir=self.spools)
            self.assertFalse(self.output.exists())
        for value in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                converter.convert(self.source, self.output, concurrency=value)
        result = subprocess.run([sys.executable, str(SCRIPT), "--input", "-", "--output", "-", "--concurrency", "0"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_input_cannot_be_overwritten(self):
        self.write_source(conversation("original", request()))
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "different files"):
            converter.convert(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_keyboard_interrupt_cleans_up_spools_and_output(self):
        self.write_source(conversation("A", request()), conversation("B", request()))
        original_spool = converter._spool_next

        def interrupt_second(*args):
            if args[-1].conversation_count == 1:
                raise KeyboardInterrupt
            return original_spool(*args)

        with patch.object(converter, "_spool_next", side_effect=interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                converter.convert(self.source, self.output, concurrency=2, temp_dir=self.spools)
        self.assertEqual(list(self.spools.iterdir()), [])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.environ.get("KVCACHE_CONVERTER_SAMPLE"), "set KVCACHE_CONVERTER_SAMPLE to the six-conversation sample")
    def test_local_six_conversation_sample(self):
        source = Path(os.environ["KVCACHE_CONVERTER_SAMPLE"])
        expected = {}

        def leaves(entries):
            for entry in entries:
                if entry["type"] == "subagent":
                    yield from leaves(entry["requests"])
                else:
                    yield entry

        with source.open() as handle:
            for conversation_index, line in enumerate(handle):
                session = json.loads(line)
                for request_index, entry in enumerate(leaves(session["requests"])):
                    expected[conversation_index, request_index] = (
                        session["id"], entry["t"], entry["api_time"], entry["in"], entry["out"], len(entry["hash_ids"]),
                    )
        stats = converter.convert(source, self.output, concurrency=2, temp_dir=self.spools)
        seen = set()
        starts, ends = {}, {}
        last_timestamp = -1
        tokens = blocks = subagent_requests = 0
        with self.output.open() as handle:
            for line in handle:
                row = json.loads(line)
                key = row["conversation_index"], row["request_index"]
                self.assertNotIn(key, seen)
                seen.add(key)
                self.assertEqual((row["conversation_id"], row["relative_timestamp"], row["api_time"],
                                  row["input_length"], row["output_length"], len(row["hash_ids"])), expected[key])
                self.assertGreaterEqual(row["timestamp"], last_timestamp)
                self.assertEqual(row["timestamp"], row["conversation_start_timestamp"] + row["relative_timestamp"])
                self.assertEqual(row["completion_timestamp"], row["timestamp"] + row["api_time"])
                self.assertEqual(row["block_size"], 64)
                last_timestamp = row["timestamp"]
                tokens += row["input_length"]
                blocks += len(row["hash_ids"])
                subagent_requests += row["agent_id"] is not None
                index = row["conversation_index"]
                starts[index] = row["conversation_start_timestamp"]
                ends[index] = max(ends.get(index, 0), row["completion_timestamp"])
        self.assertEqual(seen, expected.keys())
        self.assertEqual((stats.conversation_count, stats.request_count), (6, 1443))
        self.assertEqual((tokens, blocks, subagent_requests), (192492104, 3008405, 224))
        for index, start in enumerate((0, 0, 1444.887, 3427.295, 5857.726, 21906.145)):
            self.assertAlmostEqual(starts[index], start, places=6)
        boundaries = sorted([(s, 1) for s in starts.values()] + [(e, -1) for e in ends.values()])
        active = 0
        for _, change in boundaries:
            active += change
            self.assertLessEqual(active, 2)
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
