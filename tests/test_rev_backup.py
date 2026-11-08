# поток: rev
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import rev_backup as backup
import rev_restore as restore


class RecoverySafetyTests(unittest.TestCase):
    def test_encryption_roundtrip_and_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key=root/'key';key.write_bytes(b'not-a-production-key\n');key.chmod(0o600)
            plain=root/'plain';plain.write_bytes(b'private recovery state'*100)
            enc=root/'bundle.gpg';backup.encrypt(plain,enc,key)
            out=root/'out';backup.decrypt(enc,out,key);self.assertEqual(out.read_bytes(),plain.read_bytes())
            raw=bytearray(enc.read_bytes());raw[-20]^=1;enc.write_bytes(raw)
            with self.assertRaises(backup.BackupError):backup.decrypt(enc,out,key)

    def test_code_archive_excludes_secrets_and_customer_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'feedback_bot').mkdir()
            for name in ('main.py','records.json','.env','customers.txt'):(root/'feedback_bot'/name).write_text('content')
            (root/'feedback_bot/private.py').symlink_to('/etc/passwd')
            dest=root/'code.tar';backup.code_snapshot(root,dest)
            with tarfile.open(dest) as archive:self.assertEqual(archive.getnames(),['feedback_bot/main.py'])

    def test_private_recursive_notes_survive_encrypted_source_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);notes=root/'docs/rev';(notes/'nested').mkdir(parents=True)
            files={'brand_notes_2026-09-21.md':'scoped brand note; provenance=original',
                   'nested/manual_rules.md':'manual rule; source=confirmed expert'}
            for name,text in files.items():(notes/name).write_text(text)
            (root/'outside.md').write_text('must not follow a link')
            (notes/'linked.md').symlink_to(root/'outside.md')
            originals={'docs/handoff/archive/rev_2026-09-10.md':'original handoff provenance',
                       'docs/reports/rev_gold_set.md':'original approved-answer provenance'}
            for name,text in originals.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
            plain=root/'code.tar';backup.code_snapshot(root,plain)
            key=root/'key';key.write_bytes(b'fixture-key-only\n');key.chmod(0o600)
            encrypted=root/'code.gpg';backup.encrypt(plain,encrypted,key)
            decoded=root/'decoded.tar';backup.decrypt(encrypted,decoded,key)
            with tarfile.open(decoded) as archive:
                expected=dict(originals,**{'docs/rev/'+name:text for name,text in files.items()})
                self.assertEqual(set(archive.getnames()),set(expected))
                for name,text in expected.items():self.assertEqual(archive.extractfile(name).read().decode(),text)

    def test_public_oauth_pages_are_recoverable_without_adjacent_private_web_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);web=root/'web/rev_backup_public';web.mkdir(parents=True)
            (web/'index.html').write_text('<h1>Rev Backup</h1>')
            (web/'credentials.json').write_text('private fixture')
            (root/'web/other.html').write_text('unrelated application')
            archive=root/'code.tar';backup.code_snapshot(root,archive)
            with tarfile.open(archive) as t:
                self.assertEqual(t.getnames(),['web/rev_backup_public/index.html'])

    def test_retention_preserves_unreceipted_and_failed_offsite_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for day in range(1,5):
                p=root/('rev-backup-2026010'+str(day)+'T000000Z.tar.gz.gpg');p.write_bytes(b'ciphertext')
                if day in (2,3):p.with_suffix('.gpg.json').write_text(json.dumps({'bundle_sha256':backup.sha256(p),'offsite':{'verified':day==2}}))
            backup.retention(root,1,0)
            self.assertTrue((root/'rev-backup-20260101T000000Z.tar.gz.gpg').exists())
            self.assertTrue((root/'rev-backup-20260102T000000Z.tar.gz.gpg').exists())
            self.assertTrue((root/'rev-backup-20260103T000000Z.tar.gz.gpg').exists())

    def test_missing_offsite_never_reports_verified(self):
        with self.assertRaises(backup.BackupError):backup.upload(Path('unused'),{})

    def test_low_space_rotation_requires_verified_offsite_and_preserves_latest(self):
        for verified in (True,False):
            with self.subTest(verified=verified),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);paths=[]
                for hour in ('010000','020000'):
                    p=root/('rev-backup-20260101T'+hour+'Z.tar.gz.gpg');p.write_bytes(b'ciphertext')
                    row={'bundle':str(p),'bundle_sha256':backup.sha256(p),'local_only':True,'offsite':{'verified':verified}}
                    p.with_suffix('.gpg.json').write_text(json.dumps(row));paths.append(p)
                (root/'latest.json').write_text(json.dumps(row))
                with patch.object(backup.shutil,'disk_usage',side_effect=lambda p:Mock(free=0 if paths[0].exists() else 10**12)):
                    with self.assertRaises(backup.BackupError):backup.ensure_backup_space(root,100)
                self.assertTrue(paths[1].exists())
                self.assertTrue(paths[0].exists())

    def test_low_space_never_rotates_if_latest_integrity_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'rev-backup-20260101T020000Z.tar.gz.gpg';p.write_bytes(b'ciphertext')
            (root/'latest.json').write_text(json.dumps({'bundle':str(p),'bundle_sha256':'mismatch','offsite':{'verified':True}}))
            with patch.object(backup.shutil,'disk_usage',return_value=Mock(free=0)),patch.object(backup,'retention') as rotation:
                with self.assertRaises(backup.BackupError):backup.ensure_backup_space(root,100)
            rotation.assert_not_called();self.assertTrue(p.exists())

    def test_verified_recent_compressed_copy_sets_staging_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'rev-backup-20260101T020000Z.tar.gz.gpg';p.write_bytes(b'fixture-ciphertext')
            (root/'latest.json').write_text(json.dumps({'bundle':str(p),'bundle_sha256':backup.sha256(p)}))
            with patch.object(backup.shutil,'disk_usage',return_value=Mock(free=1500*1024*1024)),patch.object(backup,'retention') as rotation:
                backup.ensure_backup_space(root,2*1024*1024*1024)
            rotation.assert_not_called();self.assertTrue(p.exists())

    def test_running_writer_stops_if_reserve_is_threatened(self):
        process=Mock();process.communicate.return_value=(b'',b'')
        with patch.object(backup.subprocess,'Popen',return_value=process),patch.object(backup.shutil,'disk_usage',return_value=Mock(free=backup.MIN_FREE_BYTES-1)):
            with self.assertRaises(backup.BackupError):backup.run(['fixture-command'],space_guard=Path('/unused'))
        process.terminate.assert_called_once();process.communicate.assert_called_once()

    def test_archive_writer_checks_reserve_before_writing(self):
        stream=io.BytesIO();writer=backup.SpaceCheckedWriter(stream,Path('/unused'))
        with patch.object(backup.shutil,'disk_usage',return_value=Mock(free=backup.MIN_FREE_BYTES+2)):
            with self.assertRaises(backup.BackupError):writer.write(b'abc')
        self.assertEqual(stream.getvalue(),b'')

    def test_ssh_checksum_mismatch_never_renames_or_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'encrypted.gpg';file.write_bytes(b'ciphertext')
            with patch.object(backup,'checked_ssh_settings',return_value=('backup@example.org','/private', '22',[])), \
                 patch.object(backup.socket,'getaddrinfo',return_value=[(None,None,None,None,('192.0.2.1',0))]), \
                 patch.object(backup,'run',side_effect=[b'127.0.0.1',b'',b'',b'wronghash  path'] ) as called:
                with self.assertRaises(backup.BackupError):backup.upload_ssh(file,{})
            self.assertFalse(any('mv --' in str(call) for call in called.call_args_list))

    def test_restore_refuses_source_and_arbitrary_names(self):
        for name in ('rev_recovery_20261103','postgres','rev_restore_drill_x;DROP DATABASE x'):
            with self.assertRaises(backup.BackupError):restore.validate_target(name,'rev_recovery_20261103')
        restore.validate_target('rev_restore_drill_20261105_test','rev_recovery_20261103')

    def test_existing_database_never_replaced(self):
        conn=Mock();conn.cursor.return_value.__enter__=Mock(return_value=conn.cursor.return_value)
        conn.cursor.return_value.__exit__=Mock(return_value=False)
        cur=conn.cursor.return_value;cur.fetchone.return_value=(1,)
        with patch.object(restore.psycopg2,'connect',return_value=conn):
            with self.assertRaises(backup.BackupError):restore.create_database({},'rev_restore_drill_test')
        self.assertEqual(cur.execute.call_count,1);conn.close.assert_called_once()

    def test_restore_blocks_traversal_and_symlink_descendant(self):
        for name in ('../../root/overwrite','/etc/passwd'):
            with self.assertRaises(backup.BackupError):restore.safe_member(tarfile.TarInfo(name))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'unsafe.tar'
            with tarfile.open(path,'w') as archive:
                link=tarfile.TarInfo('runtime/venv/x');link.type=tarfile.SYMTYPE;link.linkname='/usr/bin/python3';archive.addfile(link)
                member=tarfile.TarInfo('runtime/venv/x/file');member.size=1;archive.addfile(member,io.BytesIO(b'x'))
            with tarfile.open(path) as archive:
                with self.assertRaises(backup.BackupError):restore.extract(archive,Path(tmp)/'dest')

    def test_ssh_config_rejects_traversal_and_shell_values(self):
        for host,directory in [('server;evil','/backup'),('valid','/backups/../root')]:
            with self.assertRaises(backup.BackupError):backup.checked_ssh_settings({'REV_BACKUP_SSH_HOST':host,'REV_BACKUP_SSH_USER':'backup','REV_BACKUP_SSH_DIR':directory})

if __name__=='__main__':unittest.main()
