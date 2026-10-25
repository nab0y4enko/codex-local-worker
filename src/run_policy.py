"""Durable per-repository workstream limits, independent of task IDs and worktrees.

State corruption and interrupted reservations fail closed. Reservations prevent
concurrent repair jobs from each spending the complete shared allowance.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

EMPTY_LIMIT = 2
REPAIR_ATTEMPTS = 2
REPAIR_SECONDS = 1200.0

class PolicyStop(RuntimeError):
    pass

class Workstream:
    def __init__(self, state_dir, repo_identity, name='default', kind='implementation'):
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',name):
            raise ValueError('workstream must be a stable 1-64 character name, without paths')
        if kind not in ('implementation','repair'):
            raise ValueError('job kind must be implementation or repair')
        self.name,self.kind=name,kind
        key=hashlib.sha256((str(repo_identity)+'\0'+name).encode()).hexdigest()
        self.path=Path(state_dir)/'workstreams'/(key+'.json')
        self.lock_path=self.path.with_suffix('.lock')

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.lock_path.open('a') as lock:
            end=time.monotonic()+2
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=end:raise PolicyStop('workstream state is busy; stop instead of starting another repair')
                    time.sleep(.01)
            try:
                if self.path.exists():
                    try:s=json.loads(self.path.read_text())
                    except (OSError,ValueError) as e:raise PolicyStop('workstream state unreadable; preserve it for review') from e
                    if not isinstance(s,dict) or s.get('version')!=1 or not isinstance(s.get('active'),dict) or any(not isinstance(s.get(k),(int,float)) or isinstance(s.get(k),bool) or s[k]<0 or not math.isfinite(s[k]) for k in ('empty_streak','repair_attempts','repair_seconds')):
                        raise PolicyStop('workstream state invalid; preserve it for review')
                else:s=dict(version=1,workstream=self.name,empty_streak=0,repair_attempts=0,repair_seconds=0.0,active={})
                if s.get('workstream') != self.name or type(s.get('version')) is not int:
                    raise PolicyStop('workstream identity invalid; preserve it for review')
                for key in ('empty_streak', 'repair_attempts'):
                    if type(s[key]) is not int:
                        raise PolicyStop('workstream counters invalid; preserve them for review')
                for reservation in s['active'].values():
                    if not isinstance(reservation, dict) or reservation.get('kind') not in ('implementation', 'repair'):
                        raise PolicyStop('workstream reservation invalid; preserve it for review')
                    for key in ('reserved_seconds', 'reserved_attempts', 'recorded', 'started'):
                        value = reservation.get(key)
                        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                            raise PolicyStop('workstream reservation invalid; preserve it for review')
                        if key != 'reserved_seconds' and type(value) is not int:
                            raise PolicyStop('workstream reservation counters invalid')
                    if reservation['recorded'] > reservation['started']:
                        raise PolicyStop('workstream completion exceeds admitted attempts')
                yield s
                fd,tmp=tempfile.mkstemp(dir=str(self.path.parent),prefix='.policy-')
                try:
                    with os.fdopen(fd,'w') as f:json.dump(s,f,indent=2);f.write('\n')
                    os.replace(tmp,self.path)
                finally:
                    if os.path.exists(tmp):os.unlink(tmp)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    @staticmethod
    def check(s):
        if s['empty_streak']>=EMPTY_LIMIT:
            raise PolicyStop('two consecutive attempts produced no scoped patch; stop, preserve evidence and reassess this workstream')
        if s['repair_attempts']>=REPAIR_ATTEMPTS or s['repair_seconds']>=REPAIR_SECONDS:
            raise PolicyStop('repair workstream exhausted its two-attempt / 1200-second allowance; no automatic renewal')

    def claim(self,job_id,requested_seconds,max_attempts):
        if not math.isfinite(requested_seconds) or requested_seconds<=0 or max_attempts not in (1,2):raise ValueError('invalid workstream reservation')
        with self.locked() as s:
            self.check(s)
            if job_id in s['active']:raise PolicyStop('job already holds a workstream reservation')
            seconds=float(requested_seconds);attempts=max_attempts
            if self.kind=='repair':
                remaining_attempts=REPAIR_ATTEMPTS-s['repair_attempts']-sum(x.get('reserved_attempts',0) for x in s['active'].values())
                remaining_seconds=REPAIR_SECONDS-s['repair_seconds']-sum(x.get('reserved_seconds',0) for x in s['active'].values())
                if remaining_attempts<=0 or remaining_seconds<=0:raise PolicyStop('repair allowance reserved by another job; no parallel renewal')
                attempts=min(attempts,remaining_attempts);seconds=min(seconds,remaining_seconds)
            s['active'][job_id]=dict(kind=self.kind,reserved_attempts=attempts if self.kind=='repair' else 0,reserved_seconds=seconds if self.kind=='repair' else 0,recorded=0,started=0,pid=os.getpid())
            return dict(job_budget_seconds=seconds,max_attempts=attempts,empty_streak=s['empty_streak'],state_path=str(self.path))

    def before_attempt(self,job_id):
        with self.locked() as s:
            self.check(s)
            if job_id not in s['active']:raise PolicyStop('workstream reservation missing')
            claim=s['active'][job_id]
            if claim['kind']=='repair' and claim.get('started',claim['recorded'])>=claim['reserved_attempts']:raise PolicyStop('repair attempt allowance exhausted')

    def start_attempt(self, job_id):
        """Charge inference admission durably even if its runner never returns."""
        with self.locked() as state:
            self.check(state)
            claim = state['active'].get(job_id)
            if claim is None:
                raise PolicyStop('workstream reservation missing')
            if claim['kind'] == 'repair' and claim.get('started', 0) >= claim['reserved_attempts']:
                raise PolicyStop('repair attempt allowance exhausted')
            claim['started'] = claim.get('started', 0) + 1

    def record_attempt(self,job_id,*,has_patch):
        with self.locked() as s:
            if job_id not in s['active']:raise PolicyStop('workstream reservation missing')
            claim=s['active'][job_id]
            # Direct callers may atomically record an already completed attempt.
            claim['started'] = max(claim.get('started', 0), claim['recorded'] + 1)
            claim['recorded']+=1
            s['empty_streak']=0 if has_patch else s['empty_streak']+1
            return dict(empty_streak=s['empty_streak'],repair_attempts=s['repair_attempts']+claim['recorded'] if claim['kind']=='repair' else s['repair_attempts'])

    def finish(self,job_id,elapsed_seconds):
        with self.locked() as s:
            claim=s['active'].pop(job_id,None)
            if claim and claim['kind']=='repair':
                s['repair_attempts']+=claim.get('started',claim['recorded'])
                s['repair_seconds']+=max(0.0,float(elapsed_seconds))
