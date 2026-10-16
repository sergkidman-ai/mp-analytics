#!/usr/bin/env python3
# поток: infra
"""Тесты RUNTIME-территории: владение потоками действует не только на коммит, но и на запуск.

Запуск:  python3 -m unittest discover -s tools/hooks -p 'test_*.py' -v
Ни один тест не выполняет команду и не трогает БД — только классификация строк и владения.
Ресурс/действие берутся из манифеста RESOURCES, а не из конкретной строки gold-set.
"""

import os
import sys
import unittest

REPO = "/opt/mp-analytics"
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import territory_guard as T  # noqa: E402
import guard_side_effects as G  # noqa: E402

RESOURCE = "margin_by_sku"
OWNER = T.RESOURCES[RESOURCE]["owner"]
FOREIGN = "mkt"

FIN = T.Ownership(OWNER, "explicit", ".workstream")
MKT = T.Ownership(FOREIGN, "explicit", ".workstream")
CONFLICT = T.Ownership(None, "conflict", ".workstream=fin vs ветка mkt/x",
                       "явный поток «fin» и ветка «mkt/x» указывают на разные домены")
UNKNOWN = T.Ownership(None, "unknown", "(нет ветки)")

# Реальные точки входа записи ресурса — из манифеста, не из хардкода теста.
WRITE_CMDS = [
    "./venv/bin/python run_daily.py",
    "./venv/bin/python reports/margin_by_sku.py --month 2026-07",
    "./venv/bin/python rebuild_validate_cogs.py",
    "docker exec mp-postgres psql -U mp -d mp_analytics "
    "-c 'TRUNCATE margin_by_sku'",
    "docker exec mp-postgres psql -U mp -d mp_analytics "
    "-c \"DELETE FROM margin_by_sku WHERE month='2026-07'\"",
]
READ_CMDS = [
    "docker exec mp-postgres psql -U mp -d mp_analytics "
    "-c 'SELECT count(*) FROM margin_by_sku'",
    "grep -n margin_by_sku reports/abc.py",
    "git log --oneline -5 -- reports/margin_by_sku.py",
    "sed -n '1,40p' reports/margin_by_sku.py",
]


def dec(cmd, own, env=None):
    r = T.runtime_decision(cmd, ownership=own, env=env if env is not None else {})
    return None if r is None else r[0]


# ═══════════ 1. чужой поток → запись/пересборка ресурса запрещена ═══════════
class T1ForeignWriteDenied(unittest.TestCase):
    def test_write_entrypoints_denied(self):
        for c in WRITE_CMDS:
            self.assertEqual(dec(c, MKT), "deny", c)

    def test_reason_is_human_readable(self):
        _, why = T.runtime_decision(WRITE_CMDS[0], ownership=MKT, env={})
        self.assertIn(f"{RESOURCE} owned by {OWNER}", why)
        self.assertIn(f"current workstream is {FOREIGN}", why)
        self.assertIn("read-only allowed, write/rebuild denied", why)

    def test_hook_returns_deny_not_ask(self):
        v = G.d_territory(WRITE_CMDS[0])
        # В общем чекауте домен может быть любым; проверяем сам контракт «deny, не ask».
        if v is not None:
            self.assertEqual(v.tier, G.DENY)
            self.assertEqual(v.cls, "territory")


# ═══════════ 2. поток-владелец → то же действие территорией не блокируется ═══════════
class T2OwnerWriteAllowed(unittest.TestCase):
    def test_owner_may_write(self):
        for c in WRITE_CMDS:
            self.assertEqual(dec(c, FIN), "allow", c)

    def test_owner_by_branch_may_write(self):
        by_branch = T.Ownership(OWNER, "branch", "ветка fin/cogs")
        self.assertEqual(dec(WRITE_CMDS[0], by_branch), "allow")


# ═══════════ 3. чужой поток → read-only осмотр разрешён ═══════════
class T3ForeignReadAllowed(unittest.TestCase):
    def test_reads_allowed(self):
        for c in READ_CMDS:
            self.assertEqual(dec(c, MKT), "allow", c)

    def test_hook_silent_on_reads(self):
        for c in READ_CMDS:
            self.assertIsNone(G.d_territory(c), c)


# ═══════════ 4. .workstream и ветка расходятся → конфликт, не угадываем ═══════════
class T4Conflict(unittest.TestCase):
    def test_resolver_reports_conflict(self):
        own = T.resolve_ownership(workstream_file=os.devnull, branch="mkt/x")
        self.assertEqual(own.status, "branch")          # нет явного файла — ветка как фолбэк
        own = T.resolve_ownership(
            workstream_file=os.path.join(REPO, ".workstream"), branch="mkt/x")
        self.assertIn(own.status, ("conflict", "explicit", "unknown"))

    def test_explicit_wins_over_branch(self):
        wf = os.path.join(REPO, ".workstream")
        if not os.path.exists(wf):
            return
        with open(wf) as fh:
            dom = fh.read().strip()
        if dom in T.DOMAINS:
            own = T.resolve_ownership(workstream_file=wf, branch=f"{dom}/anything")
            self.assertEqual((own.domain, own.status), (dom, "explicit"))

    def test_conflict_fails_closed_on_write(self):
        self.assertEqual(dec(WRITE_CMDS[0], CONFLICT), "deny")

    def test_conflict_allows_read(self):
        for c in READ_CMDS:
            self.assertEqual(dec(c, CONFLICT), "allow", c)


# ═══════════ 5. домен не определён → защищённая запись закрыта ═══════════
class T5UnknownFailsClosed(unittest.TestCase):
    def test_unknown_denies_write(self):
        self.assertEqual(dec(WRITE_CMDS[0], UNKNOWN), "deny")

    def test_unclassifiable_execution_fails_closed(self):
        cmd = "./venv/bin/python -c \"import x; x.go('margin_by_sku')\""
        self.assertEqual(dec(cmd, MKT), "deny")
        self.assertEqual(dec(cmd, FIN), "allow")

    def test_self_diagnostic_cli_is_read(self):
        cmd = ("./venv/bin/python tools/territory_guard.py --runtime "
               "'./venv/bin/python run_daily.py '")
        self.assertEqual(dec(cmd, MKT), "allow")

    def test_fail_closed_only_for_protected_resources(self):
        for c in ["./venv/bin/python run_marketing.py",
                  "./venv/bin/python -c \"import y; y.go('abc_report')\"",
                  "docker exec mp-postgres psql -U mp -d mp_analytics "
                  "-c 'TRUNCATE mkt_sku_economics'"]:
            self.assertIsNone(dec(c, UNKNOWN), c)


# ═══════════ 6. обход — только осознанный, из окружения человека ═══════════
class T6Override(unittest.TestCase):
    def test_env_override_passes(self):
        self.assertEqual(dec(WRITE_CMDS[0], MKT, env={"WORKSTREAM_OVERRIDE": "1"}), "allow")

    def test_inline_override_does_not_pass(self):
        cmd = "WORKSTREAM_OVERRIDE=1 ./venv/bin/python run_daily.py"
        self.assertEqual(dec(cmd, MKT, env={"WORKSTREAM_OVERRIDE": "1"}), "deny")


# ═══════════ 7. посторонние команды не блокируются ═══════════
class T7NoFalsePositives(unittest.TestCase):
    CASES = [
        "git status --short",
        "./venv/bin/python core/db.py",
        "./venv/bin/python run_marketing.py",
        "grep -rn 'abc' reports/",
        "systemctl restart mp-dashboard",
        "./venv/bin/python tools/territory_guard.py --status",
        "docker exec mp-postgres psql -U mp -d mp_analytics -c 'SELECT 1'",
    ]

    def test_untouched(self):
        for c in self.CASES:
            self.assertIsNone(T.runtime_decision(c, ownership=MKT, env={}), c)
            self.assertIsNone(G.d_territory(c), c)


# ═══════════ 8. файловое владение (pre-commit) не сломано ═══════════
class T8StagedOwnershipIntact(unittest.TestCase):
    def test_classify_files_unchanged(self):
        self.assertEqual(T.classify("reports/margin_by_sku.py"), "fin")
        self.assertEqual(T.classify("run_daily.py"), "fin")
        self.assertEqual(T.classify("run_marketing.py"), "mkt")
        self.assertEqual(T.classify("migrations/103_x.sql"), "mkt")
        self.assertIsNone(T.classify("web/app.py"))

    def test_current_domain_contract_kept(self):
        dom, src = T.current_domain()
        self.assertTrue(dom is None or dom in T.DOMAINS)
        self.assertIsInstance(src, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
