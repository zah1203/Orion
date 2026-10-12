from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sqlite3
from contextlib import closing

from scripts.portal_release import activate, DROPIN, MARKER

SHA = 'a'*40
OLD = 'b'*40


class PortalReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.release=self.root/'opt/orion/releases'/SHA
        (self.release/'.venv/bin').mkdir(parents=True)
        (self.release/'.venv/bin/python').touch()
        self.old=self.root/'opt/orion/releases'/OLD; self.old.mkdir()
        (self.root/'opt/orion/current').symlink_to(self.old)
        self.dropin=self.root/DROPIN
        self.calls=[]; self.restarts=0; self.states=0
        self.active=True; self.fail_restart=False; self.change_worker=False
        self.health_calls=0; self.fail_health=False
        state=self.root/'var/lib/orion/portal'; state.mkdir(parents=True)
        with closing(sqlite3.connect(state/'accounts.db')) as db:
            db.execute('CREATE TABLE users(id TEXT)')
        self.original_accounts = (state/'accounts.db').read_bytes()
        (state/'paper.db').write_bytes(b'unchanged positions')

    def fake_run(self,args,**kwargs):
        self.calls.append(args)
        out=''; code=0
        if args[1]=='is-active': code=0 if self.active else 3
        elif args[1]=='list-units':
            out='orion-supervisor.service loaded active running Supervisor\norion-worker@test.service loaded active running Worker\n'
        elif args[1]=='show':
            if args[2]=='orion-portal.service': out='/opt/orion/releases/'+SHA+'\n'
            else:
                self.states+=1
                pid='22' if self.change_worker and self.states>2 else '11'
                out='MainPID='+pid+'\nInvocationID=synthetic\nActiveState=active\n'
        elif args[1]=='restart':
            self.restarts+=1
            if self.fail_restart and self.restarts==1: raise RuntimeError('restart failed')
        return SimpleNamespace(stdout=out,returncode=code)

    def health(self,root):
        self.assertEqual(root,self.root); self.health_calls+=1
        if self.fail_health and self.health_calls==1: raise RuntimeError('unhealthy')

    def activate(self): activate(SHA,root=self.root,run=self.fake_run,health=self.health)

    def assert_paper_untouched(self):
        self.assertEqual((self.root/'opt/orion/current').resolve(),self.old)
        state=self.root/'var/lib/orion/portal'
        self.assertEqual((state/'accounts.db').read_bytes(),self.original_accounts)
        self.assertEqual((state/'paper.db').read_bytes(),b'unchanged positions')
        mutations=[a for a in self.calls if a[1] in ('stop','start','restart','enable','disable')]
        self.assertTrue(all(a==['systemctl','restart','orion-portal.service'] for a in mutations))

    def test_portal_pinned_workers_and_state_untouched(self):
        self.activate()
        self.assertIn('WorkingDirectory=/opt/orion/releases/'+SHA,self.dropin.read_text())
        self.assertIn('ExecStart=\nExecStart=',self.dropin.read_text())
        self.assertEqual(self.restarts,1); self.assert_paper_untouched()

    def test_first_deploy_health_failure_removes_override_and_restores_portal(self):
        self.fail_health=True
        with self.assertRaisesRegex(RuntimeError,'previous portal configuration restored'): self.activate()
        self.assertFalse(self.dropin.exists()); self.assertEqual(self.restarts,2)
        self.assert_paper_untouched()

    def test_later_deploy_failure_restores_previous_override_exactly(self):
        previous=(MARKER+'[Service]\nWorkingDirectory=/old\n').encode()
        self.dropin.parent.mkdir(parents=True); self.dropin.write_bytes(previous)
        self.fail_restart=True
        with self.assertRaisesRegex(RuntimeError,'restored'): self.activate()
        self.assertEqual(self.dropin.read_bytes(),previous); self.assert_paper_untouched()

    def test_worker_restart_detected_and_portal_rolled_back(self):
        self.change_worker=True
        with self.assertRaisesRegex(RuntimeError,'restored'): self.activate()
        self.assertEqual(self.restarts,2); self.assert_paper_untouched()

    def test_inactive_portal_and_unmanaged_override_refused_before_mutation(self):
        self.active=False
        with self.assertRaisesRegex(RuntimeError,'running portal'): self.activate()
        self.assertFalse(self.dropin.exists()); self.assertEqual(self.restarts,0)
        self.active=True; self.dropin.parent.mkdir(parents=True)
        self.dropin.write_text('manual configuration')
        with self.assertRaisesRegex(RuntimeError,'requires review'): self.activate()
        self.assertEqual(self.dropin.read_text(),'manual configuration')
        self.assertEqual(self.restarts,0)

    def test_symlink_override_and_invalid_release_refused(self):
        self.dropin.parent.mkdir(parents=True)
        self.dropin.symlink_to(self.root/'unrelated')
        with self.assertRaisesRegex(RuntimeError,'symlink'): self.activate()
        with self.assertRaises(ValueError): activate('../escape',root=self.root,run=self.fake_run)
        self.assertEqual(self.calls,[])

    def test_failed_rollback_is_explicit(self):
        def unhealthy(root): raise RuntimeError('unhealthy')
        with self.assertRaisesRegex(RuntimeError,'rollback needs operator review'):
            activate(SHA,root=self.root,run=self.fake_run,health=unhealthy)
        self.assertEqual(self.restarts,2); self.assert_paper_untouched()

    def test_reserved_live_slot_or_corrupt_database_blocks_all_service_changes(self):
        path=self.root/'var/lib/orion/portal/accounts.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE live_worker_slots(reserved INTEGER)')
            db.execute('INSERT INTO live_worker_slots VALUES(1)'); db.commit()
        with self.assertRaisesRegex(RuntimeError,'reserved'): self.activate()
        self.assertFalse(self.dropin.exists()); self.assertEqual(self.calls,[])
        path.write_bytes(b'corrupt fixture')
        with self.assertRaisesRegex(RuntimeError,'preflight'): self.activate()
        self.assertEqual(self.calls,[])
