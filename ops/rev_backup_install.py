#!/usr/bin/env python3
# поток: rev
"""Apply only the approved Rev backup timer; preserve all other unit definitions."""
from pathlib import Path
import datetime,hashlib,json,os,shutil,subprocess

ROOT=Path(__file__).resolve().parents[1]
STATE=Path('/root/rev-recovery/backup-maintenance-20261108')
OTHERS=('feedback-recovery-cycle.timer','feedback-recovery-review.timer',
        'feedback-rev-delivery.timer','feedback-rev-git-sync.timer',
        'feedback-moderation.service','feedback-recovery-cycle.service',
        'feedback-recovery-review.service','feedback-rev-delivery.service','feedback-rev-git-sync.service')

def execute(args):return subprocess.check_output(args,cwd=ROOT,stderr=subprocess.DEVNULL,text=True)
def signature(name):
    fields=dict(x.split('=',1) for x in execute(['systemctl','show',name,'--property=FragmentPath,ActiveState,UnitFileState']).splitlines() if '=' in x)
    fields['sha256']=hashlib.sha256(Path(fields['FragmentPath']).read_bytes()).hexdigest()
    return fields

def main():
    os.umask(0o077)
    if Path.cwd()!=ROOT or os.geteuid()!=0:raise RuntimeError('Run from the Rev checkout as its local operator')
    source=ROOT/'feedback_bot/feedback-rev-backup.timer'
    text=source.read_text()
    if 'OnCalendar=*-*-* 01:30:00 UTC' not in text or 'OnCalendar=hourly' in text or 'RandomizedDelaySec=0' not in text:raise RuntimeError('Unapproved backup schedule')
    execute(['systemd-analyze','verify',str(source)])
    before={name:signature(name) for name in OTHERS};STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=Path('/etc/systemd/system/feedback-rev-backup.timer')
    rollback=STATE/'feedback-rev-backup.timer.before'
    if not rollback.exists():shutil.copy2(target,rollback)
    if target.read_bytes()!=source.read_bytes():
        tmp=target.with_name(target.name+'.rev-new');shutil.copyfile(source,tmp);tmp.chmod(0o644);os.replace(tmp,target)
        execute(['systemctl','daemon-reload']);execute(['systemctl','restart','feedback-rev-backup.timer'])
    after={name:signature(name) for name in OTHERS}
    for name,old in before.items():
        assert old['sha256']==after[name]['sha256'] and old['UnitFileState']==after[name]['UnitFileState']
        if name.endswith('.timer'):assert old['ActiveState']==after[name]['ActiveState']
    proof={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'schedule':'01:30 UTC daily',
        'other_units_unchanged':True,'backup_timer':signature('feedback-rev-backup.timer'),
        'next_run':execute(['systemctl','show','feedback-rev-backup.timer','--property=NextElapseUSecRealtime']).strip()}
    (STATE/'timer-proof.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))

if __name__=='__main__':main()
