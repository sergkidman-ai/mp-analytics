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

    def test_retention_preserves_unreceipted_and_failed_offsite_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for day in range(1,5):
                p=root/('rev-backup-2026010'+str(day)+'T000000Z.tar.gz.gpg');p.write_bytes(b'ciphertext')
                if day in (2,3):p.with_suffix('.gpg.json').write_text(json.dumps({'bundle_sha256':backup.sha256(p),'offsite':{'verified':day==2}}))
            backup.retention(root,1,0)
            self.assertTrue((root/'rev-backup-20260101T000000Z.tar.gz.gpg').exists())
            self.assertFalse((root/'rev-backup-20260102T000000Z.tar.gz.gpg').exists())
            self.assertTrue((root/'rev-backup-20260103T000000Z.tar.gz.gpg').exists())

    def test_missing_offsite_never_reports_verified(self):
        with self.assertRaises(backup.BackupError):backup.upload(Path('unused'),{})

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
