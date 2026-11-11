# поток: inv
import hashlib
import stat
from pathlib import Path
import pytest
from collectors import bank_file_import as importer


@pytest.fixture
def source(monkeypatch,tmp_path):
    root=tmp_path/'archive'
    monkeypatch.setenv('BANK_STATEMENT_ARCHIVE_DIR',str(root))
    monkeypatch.setattr(importer,'parse',lambda *a: ([{'operation_date':'2026-01-05','amount':10.01,'direction':'DEBIT'}],'test-account'))
    monkeypatch.setattr(importer.bank_txn_store,'store',lambda *a,**kw:dict(seen=1,stored=1,dup=0,before_cutoff=0,ruled=0))
    return root


def test_confirmed_source_kept_with_private_permissions(source):
    data=b'original statement bytes'
    info=importer.import_bytes(data,'statement.txt','ozon','7807355364')
    path=source/'ozon'/'7807355364'/(hashlib.sha256(data).hexdigest()+'.txt')
    assert path.read_bytes()==data
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert path.with_suffix('.txt.json').exists()
    assert info['source_sha256']==hashlib.sha256(data).hexdigest()


def test_dry_run_does_not_archive_or_store(source,monkeypatch):
    monkeypatch.setattr(importer.bank_txn_store,'store',lambda *a,**kw:pytest.fail('dry writes'))
    importer.import_bytes(b'preview','statement.txt','ozon','7807355364',dry=True)
    assert not source.exists()


def test_same_source_is_idempotent(source):
    for _ in range(2): importer.import_bytes(b'same','statement.txt','ozon','7807355364')
    assert len(list(source.rglob('*.txt')))==1
    assert len(list(source.rglob('*.json')))==1


def test_source_survives_database_error_and_path_is_safe(source,monkeypatch):
    def fail(*a,**kw):raise RuntimeError('database error')
    monkeypatch.setattr(importer.bank_txn_store,'store',fail)
    with pytest.raises(RuntimeError):
        importer.import_bytes(b'replay','../../statement.txt','../ozon','../../7807355364')
    files=list(source.rglob('*.txt'));assert len(files)==1
    assert files[0].read_bytes()==b'replay'
    assert source.resolve() in files[0].resolve().parents
