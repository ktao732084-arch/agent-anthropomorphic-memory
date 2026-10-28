"""Long-conversation regressions. All examples are synthetic, no network/API keys."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "legacy"))
from lexical_search import rank, tokenize
from collect_from_sessions import extract_messages, collect_from_sessions, generate_id
import memory


def record(id, text, **extra):
    return {"id": id, "content": text, "type": "fact", "score": 0.5,
            "importance": 0.5, "entities": [], "created": "2023-01-01T00:00:00Z", **extra}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class BM25Tests(unittest.TestCase):
    def test_short_answer_beats_long_repeated_instructions(self):
        short = record("answer", "测试服务端口是 8765，分支是 feat/synthetic-test。")
        long = record("noise", ("测试服务的端口和分支配置需要检查。" * 150))
        for q in ("服务端口", "feat/synthetic-test", "8765", "synthetic-test"):
            self.assertEqual(rank(q, [long, short])[0]["id"], "answer")

    def test_rare_term_beats_common_words(self):
        docs = [record(str(i), "project server notes") for i in range(15)]
        docs += [record("answer", "observatory"), record("noise", "project server notes " * 30)]
        self.assertEqual(rank("project server observatory", docs)[0]["id"], "answer")

    def test_english_words_and_identifiers(self):
        docs = [record("a", "I visited Cambridge museum."), record("b", "A bridge over the river.")]
        self.assertEqual([r["id"] for r in rank("CAMBRIDGE", docs)], ["a"])
        self.assertEqual([r["id"] for r in rank("bridge", docs)], ["b"])
        self.assertIn("8765", tokenize("端口8765"))
        self.assertEqual(rank("???", docs), [])
        self.assertEqual(rank("museum", []), [])

    def test_repetition_is_saturated(self):
        rows = rank("museum", [record("once", "museum"), record("many", "museum " * 100)])
        scores = {r["id"]: r["bm25_score"] for r in rows}
        self.assertLess(scores["many"] / scores["once"], 2)

    def test_pending_and_old_active_share_scoring_without_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            target = record("f_target", "测试服务端口 8765", score=1e-20)
            write_rows(path / "layer2/active/facts.jsonl", [target])
            noise = [record(f"p_{i}", "服务端口配置检查。" + "构建任务需要核对文件路径和环境变量。" * 100,
                            importance=1) for i in range(15)]
            write_rows(path / "layer2/pending.jsonl", noise)
            with patch.dict(os.environ, {"MEMORY_DIR": tmp, "MEMORY_NOW": "2026-10-29T00:00:00Z"}):
                memory._session_cache.clear()
                result = memory.router_search("服务端口", path, use_qmd=False, use_vector=False)
            self.assertEqual(result["results"][0]["id"], "f_target")


class CollectorTests(unittest.TestCase):
    def test_assistant_quotes_have_separate_ids_and_do_not_verify_user_beliefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            segments = [
                {"content": "我喜欢天文学", "source": "assistant", "importance": 0.8},
                {"content": "AI 回复：我喜欢天文学", "source": "user", "importance": 0.8},
            ]
            rows = memory.template_extract(segments, use_llm_fallback=False, memory_dir=Path(tmp))["facts"]
            self.assertEqual(len({r["id"] for r in rows}), 2)
            ai = record("ai", "astronomy is interesting", source="assistant", entities=["topic"])
            belief = record("belief", "astronomy is interesting", entities=["topic"], confidence=0.6)
            self.assertEqual(memory.code_verify_belief(belief, [ai])[0], "unchanged")
            self.assertEqual(memory.generate_summaries([ai], [], trigger_count=1), [])

    def test_default_roles_text_only_and_user_only_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions, mem = root / "sessions", root / "memory"
            mem.mkdir()
            ts = "2026-10-29T00:00:00Z"
            def msg(role, content):
                return {"type": "message", "timestamp": ts, "message": {"role": role, "content": content}}
            rows = [msg("user", "我喜欢木星"), msg("assistant", "我喜欢木星"),
                    msg("assistant", [{"type": "thinking", "thinking": "internal secret"},
                                      {"type": "toolCall", "name": "exec", "arguments": {}},
                                      {"type": "text", "text": "建议阅读天文学入门。"}]),
                    msg("toolResult", "tool output"), msg("assistant", "HEARTBEAT_OK")]
            file = sessions / "s.jsonl"
            write_rows(file, rows)
            self.assertEqual([m["source"] for m in extract_messages(file, None)], ["user", "assistant", "assistant"])
            self.assertEqual(len(extract_messages(file, None, False)), 1)
            self.assertNotEqual(generate_id("same", ts), generate_id("same", ts, "assistant"))
            with patch.dict(os.environ, {"OPENCLAW_SESSIONS_DIR": str(sessions), "MEMORY_DIR": str(mem)}):
                self.assertEqual(collect_from_sessions(hours=100000), 3)
                self.assertEqual(collect_from_sessions(hours=100000), 0)
            saved = memory.load_jsonl(mem / "layer2/pending.jsonl")
            self.assertEqual([r["source"] for r in saved], ["user", "assistant", "assistant"])
            self.assertNotIn("internal secret", json.dumps(saved))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "MEMORY_"))}
        self.env.update(MEMORY_DIR=str(self.path), PYTHONUTF8="1", MEMORY_LLM_ENABLED="false",
                        MEMORY_LLM_JUDGE="off", MEMORY_EMBEDDING_PROVIDER="none", MEMORY_NOW="2026-10-29T00:00:00Z")
        self.cli("init")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        p = subprocess.run([sys.executable, str(ROOT / "src/memory.py"), *args], env=self.env,
                           capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_assistant_recall_does_not_override_user(self):
        self.cli("capture", "记住，我喜欢喝红茶")
        self.cli("add-pending", "更正：我不再喜欢喝红茶，改喝绿茶", "--source", "assistant")
        pending = json.loads(self.cli("search", "绿茶", "--json"))["results"]
        self.assertEqual(pending[0]["role"], "assistant")
        self.assertTrue(pending[0]["content"].startswith("AI 回复："))
        self.cli("consolidate", "--force")
        rows = memory.load_jsonl(self.path / "layer2/active/facts.jsonl")
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(r.get("invalid_at") for r in rows))
        result = json.loads(self.cli("search", "绿茶", "--json"))["results"]
        self.assertEqual(result[0]["source"], "assistant")
        self.assertTrue(result[0]["content"].startswith("AI 回复："))
        self.assertIn("AI 回复：", (self.path / "layer1/snapshot.md").read_text(encoding="utf-8"))

    def test_decay_ignores_legacy_threshold_but_manual_archive_still_works(self):
        cfg_path = self.path / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["thresholds"]["archive"] = 0.99
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        for pool, singular in (("facts", "fact"), ("beliefs", "belief"), ("summaries", "summary")):
            write_rows(self.path / f"layer2/active/{pool}.jsonl",
                       [record(f"{singular[0]}_old", "古老天文台记录", type=singular)])
        self.cli("consolidate", "--force", "--phase", "5")
        for pool in ("facts", "beliefs", "summaries"):
            rows = memory.load_jsonl(self.path / f"layer2/active/{pool}.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertLess(rows[0]["score"], 0.05)
            self.assertEqual(memory.load_jsonl(self.path / f"layer2/archive/{pool}.jsonl"), [])
        found = json.loads(self.cli("search", "天文台", "--json"))["results"]
        self.assertEqual(len(found), 3)
        self.assertIn("summary", {r["type"] for r in found})
        self.cli("archive", "f_old")
        found = json.loads(self.cli("search", "天文台", "--json"))["results"]
        self.assertNotIn("f_old", {r["id"] for r in found})


if __name__ == "__main__":
    unittest.main(verbosity=2)
