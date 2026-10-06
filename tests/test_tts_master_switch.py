import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

import intel_worker as iw  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from intel_worker import IntelWorker  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class TtsMasterSwitchTests(unittest.TestCase):
    """TTS 总闸 SYSTEM_TTS_ENABLED：引擎更换改进完成前所有 TTS 逻辑不工作"""

    def test_default_master_switch_is_off(self):
        self.assertFalse(config.SYSTEM_TTS_ENABLED)

    def test_intel_tts_configured_false_when_master_off(self):
        from intel_tts import intel_tts_service
        with patch.object(config, "RAGFLOW_TTS_ENABLED", True), \
             patch.object(config, "INTEL_TTS_BASE_URL", "http://tts.example/"), \
             patch.object(config, "INTEL_TTS_API_KEY", "k"), \
             patch.object(config, "SYSTEM_TTS_ENABLED", False):
            self.assertFalse(intel_tts_service.configured)
        with patch.object(config, "RAGFLOW_TTS_ENABLED", True), \
             patch.object(config, "INTEL_TTS_BASE_URL", "http://tts.example/"), \
             patch.object(config, "INTEL_TTS_API_KEY", "k"), \
             patch.object(config, "SYSTEM_TTS_ENABLED", True):
            self.assertTrue(intel_tts_service.configured)

    def test_remote_client_submit_forces_tts_false_when_master_off(self):
        from remote_pipeline_client import remote_pipeline_client
        with patch.object(config, "REMOTE_PIPELINE_TTS", True), \
             patch.object(config, "SYSTEM_TTS_ENABLED", False), \
             patch.object(remote_pipeline_client.session, "post") as post_mock, \
             patch.object(remote_pipeline_client, "base_url", "http://vpn.example/"), \
             patch.object(remote_pipeline_client, "token", "t" * 40):
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = {"job_id": "j1"}
            post_mock.return_value.raise_for_status.return_value = None
            remote_pipeline_client.submit(
                url="https://x.example/a", mode="article",
                keywords=["k"], limit=1,
            )
            payload = post_mock.call_args.kwargs["json"]
            self.assertFalse(payload["tts"])

    def test_remote_client_enrich_forces_tts_false_when_master_off(self):
        from remote_pipeline_client import remote_pipeline_client
        with patch.object(config, "REMOTE_PIPELINE_TTS", True), \
             patch.object(config, "SYSTEM_TTS_ENABLED", False), \
             patch.object(remote_pipeline_client.session, "post") as post_mock, \
             patch.object(remote_pipeline_client, "base_url", "http://vpn.example/"), \
             patch.object(remote_pipeline_client, "token", "t" * 40):
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = {"success": True, "refined_content": "x"}
            post_mock.return_value.raise_for_status.return_value = None
            remote_pipeline_client.enrich(
                url="https://x.example/a", title="t", content="c", tts=True,
            )
            payload = post_mock.call_args.kwargs["json"]
            self.assertFalse(payload["tts"])

    def test_handle_enrich_passes_tts_false_when_master_off(self):
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            db = SQLiteDatabase(os.path.join(tmp.name, "tts.sqlite3"))
            self.assertTrue(db.connect())
            self.assertTrue(db.create_tables())
            db.analyze_article_spacetime_profile = lambda _article_id: None
            repo = IntelRepository(db)
            worker = IntelWorker(repository=repo, worker_id="tts-master-test")
            captured = {}

            def fake_enrich(**kwargs):
                captured.update(kwargs)
                return {"refined_content": "精炼正文内容", "job_id": "j1"}

            with patch.object(iw.config, "REMOTE_PIPELINE_URL", "http://vpn.example/"), \
                 patch.object(iw.config, "REMOTE_PIPELINE_TOKEN", "t" * 40), \
                 patch.object(iw.config, "REMOTE_PIPELINE_ENRICH", True), \
                 patch.object(iw.config, "REMOTE_PIPELINE_TTS", True), \
                 patch.object(iw.config, "SYSTEM_TTS_ENABLED", False), \
                 patch.object(iw.config, "REMOTE_PIPELINE_TTS_VOICE", "default"), \
                 patch("remote_pipeline_client.remote_pipeline_client.enrich", side_effect=fake_enrich), \
                 patch("remote_result_ingestor._write_article_derivative", return_value=None), \
                 patch("redis.Redis", side_effect=RuntimeError("unit test: redis 不可用")):
                # Redis 只是 _handle_enrich 的可选加速器（代码里有「redis 不可用则跳过，
                # 回落 semaphore 兜底」分支）。本机 6379 无人监听且网络被沙箱丢包时，
                # redis-py 会按 socket_connect_timeout=2 重试 11 次，单测白等 49 秒，
                # 所以按「不可用」分支跑，既快又不依赖外部服务。
                aid = db.insert_article({
                    "url": "https://x.example/tts",
                    "title": "TTS 总闸测试",
                    # 入库闸门会判废「去框架后有效字数 < 40」的正文（intel_boilerplate.assess），
                    # 早期夹具只有 12 字，insert_article 直接返回 None，
                    # 于是 _handle_enrich 收到 article_id=0 而失败。这里给一段真实长度的正文。
                    "content": (
                        "在制造业质检环节，视觉模型已经把表面缺陷的识别时间从人工抽检的分钟级压缩到毫秒级；"
                        "同一批模型还能根据设备振动数据预测刀具磨损，在机器真正停机之前给出维护建议。"
                        "三条产线的实测数据显示，这套流程在一年内把非计划停机时间降低了约两成。"
                    ),
                    "matched_keywords": ["TTS"],
                    "publish_date": "2026-09-21",
                })
                result = worker._handle_enrich({
                    "article_id": aid,
                    "url": "https://x.example/tts",
                    "title": "TTS 总闸测试",
                    "content": "",
                })
            self.assertTrue(result.get("success"))
            self.assertFalse(captured.get("tts"))
        finally:
            if db is not None:
                db.disconnect()
            tmp.cleanup()

    def test_vpn_generate_audio_refused_when_tts_disabled(self):
        import remote_pipeline.app as app
        if getattr(app, "TTS_ENABLED", False):
            self.skipTest("本机 VPN 环境 TTS_ENABLED=1")
        with self.assertRaises(ValueError):
            app._generate_audio("job", 0, "文本", "zh", "refined")

    def test_speech_apis_rejected_when_master_off(self):
        from intel_api import _tts_master_disabled
        with patch.object(config, "SYSTEM_TTS_ENABLED", False):
            self.assertTrue(_tts_master_disabled())
        with patch.object(config, "SYSTEM_TTS_ENABLED", True):
            self.assertFalse(_tts_master_disabled())


class EmptyAudioManifestSkipTests(unittest.TestCase):
    """TTS 关闭时不再写空音频清单行"""

    def test_no_audio_row_when_manifest_empty(self):
        from remote_result_ingestor import _write_article_derivative, ensure_remote_pipeline_schema
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            db = SQLiteDatabase(os.path.join(tmp.name, "deriv.sqlite3"))
            self.assertTrue(db.connect())
            self.assertTrue(db.create_tables())
            ensure_remote_pipeline_schema(db)
            article_id = db.insert_article({
                "url": "https://x.example/deriv",
                "title": "衍生数据测试",
                # 同上：正文过短会被入库闸门判废，insert_article 返回 None，
                # 断言就会「因为什么都没写」而空过。这里给真实长度的正文。
                "content": (
                    "精炼管线的产出需要落到衍生表里：摘要、译文与音频清单各占一行，"
                    "由 article_id 关联回文章主表，便于展示层按需降级。"
                    "当 TTS 总闸关闭时音频清单为空，此时不写任何音频行，避免留下空记录。"
                ),
                "matched_keywords": ["衍生"],
                "publish_date": "2026-09-21",
            })
            _write_article_derivative(db, article_id, {
                "url": "https://x.example/deriv",
                "raw_content": "",
                "refined_content": "精炼正文",
                "refined_title": "标题",
                "translated_content": "",
                "audio_manifest": {},
                "source_language": "zh",
                "target_language": "en",
            }, "job-1")
            cursor = db.connection.cursor()
            cursor.execute("SELECT count(*) AS n FROM article_audio_manifests WHERE article_id=?", (article_id,))
            self.assertEqual(cursor.fetchone()["n"], 0)
            cursor.close()
        finally:
            if db is not None:
                db.disconnect()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
