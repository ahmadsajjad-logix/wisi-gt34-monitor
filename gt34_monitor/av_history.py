from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1

class AVHistoryDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self._schema()

    def _schema(self):
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS av_runs(
          id INTEGER PRIMARY KEY,
          started_utc TEXT NOT NULL,
          finished_utc TEXT,
          interval_seconds REAL NOT NULL,
          confirm_loss INTEGER NOT NULL,
          confirm_recovery INTEGER NOT NULL,
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS av_module_observations(
          id INTEGER PRIMARY KEY,
          run_id INTEGER NOT NULL REFERENCES av_runs(id) ON DELETE CASCADE,
          interval_no INTEGER NOT NULL,
          observed_utc TEXT NOT NULL,
          host TEXT NOT NULL,
          module INTEGER NOT NULL,
          verified_remote TEXT,
          binding_status TEXT NOT NULL,
          binding_validation_mode TEXT,
          snapshot_fresh INTEGER,
          snapshot_fresh_reason TEXT,
          UNIQUE(run_id, interval_no, host, module)
        );
        CREATE TABLE IF NOT EXISTS av_carrier_observations(
          id INTEGER PRIMARY KEY,
          module_observation_id INTEGER NOT NULL REFERENCES av_module_observations(id) ON DELETE CASCADE,
          channel INTEGER NOT NULL,
          input_name TEXT,
          configured INTEGER NOT NULL,
          carrier_locked INTEGER,
          ts_present INTEGER,
          mapping_state TEXT,
          pidmapper_input_id TEXT,
          mapping_reason TEXT,
          verdict TEXT NOT NULL,
          suppression_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS av_service_observations(
          id INTEGER PRIMARY KEY,
          carrier_observation_id INTEGER NOT NULL REFERENCES av_carrier_observations(id) ON DELETE CASCADE,
          sid INTEGER,
          service_name TEXT,
          service_verdict TEXT NOT NULL,
          video_declared INTEGER NOT NULL,
          audio_declared INTEGER NOT NULL,
          video_pids_json TEXT NOT NULL,
          audio_pids_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS av_component_observations(
          id INTEGER PRIMARY KEY,
          service_observation_id INTEGER NOT NULL REFERENCES av_service_observations(id) ON DELETE CASCADE,
          component TEXT NOT NULL,
          immediate_observation TEXT NOT NULL,
          persistent_state TEXT NOT NULL,
          inactive_streak INTEGER NOT NULL,
          recovery_streak INTEGER NOT NULL,
          last_good_utc TEXT,
          last_bad_utc TEXT,
          last_seen_utc TEXT,
          UNIQUE(service_observation_id, component)
        );
        CREATE TABLE IF NOT EXISTS av_pid_evidence(
          id INTEGER PRIMARY KEY,
          component_observation_id INTEGER NOT NULL REFERENCES av_component_observations(id) ON DELETE CASCADE,
          pid INTEGER NOT NULL,
          pid_state TEXT NOT NULL,
          bitrate REAL,
          count_delta INTEGER,
          scrambled INTEGER,
          stream_type TEXT,
          language TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_av_carrier_scope ON av_carrier_observations(channel);
        CREATE INDEX IF NOT EXISTS ix_av_module_scope ON av_module_observations(host,module,observed_utc);
        CREATE INDEX IF NOT EXISTS ix_av_service_sid ON av_service_observations(sid);
        """)
        self.connection.commit()

    def begin_run(self, started_utc: str, interval_seconds: float, confirm_loss: int, confirm_recovery: int, notes: str = "") -> int:
        cur = self.connection.execute(
            "INSERT INTO av_runs(started_utc,interval_seconds,confirm_loss,confirm_recovery,notes) VALUES(?,?,?,?,?)",
            (started_utc, interval_seconds, confirm_loss, confirm_recovery, notes),
        )
        self.connection.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_utc: str):
        self.connection.execute("UPDATE av_runs SET finished_utc=? WHERE id=?", (finished_utc, run_id))
        self.connection.commit()

    def persist_module(self, *, run_id:int, interval_no:int, observed_utc:str, host:str, module:int,
                       verified_remote:str|None, binding_status:str, binding_validation_mode:str|None,
                       snapshot_fresh:bool|None, snapshot_fresh_reason:str|None) -> int:
        cur=self.connection.execute("""INSERT INTO av_module_observations(
          run_id,interval_no,observed_utc,host,module,verified_remote,binding_status,binding_validation_mode,
          snapshot_fresh,snapshot_fresh_reason) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (run_id,interval_no,observed_utc,host,module,verified_remote,binding_status,binding_validation_mode,
           None if snapshot_fresh is None else int(snapshot_fresh),snapshot_fresh_reason))
        return int(cur.lastrowid)

    def persist_carrier(self, *, module_observation_id:int, channel:int, input_name:str|None, configured:bool,
                        carrier_locked:bool|None, ts_present:bool|None, mapping_state:str|None,
                        pidmapper_input_id:object, mapping_reason:str|None, verdict:str,
                        suppression_reason:str|None) -> int:
        cur=self.connection.execute("""INSERT INTO av_carrier_observations(
          module_observation_id,channel,input_name,configured,carrier_locked,ts_present,mapping_state,
          pidmapper_input_id,mapping_reason,verdict,suppression_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (module_observation_id,channel,input_name,int(configured),
           None if carrier_locked is None else int(carrier_locked),None if ts_present is None else int(ts_present),
           mapping_state,None if pidmapper_input_id is None else str(pidmapper_input_id),mapping_reason,verdict,suppression_reason))
        return int(cur.lastrowid)

    def persist_service(self, *, carrier_observation_id:int, sid:int|None, service_name:str,
                        service_verdict:str, video_declared:bool, audio_declared:bool,
                        video_pids:Iterable, audio_pids:Iterable) -> int:
        cur=self.connection.execute("""INSERT INTO av_service_observations(
          carrier_observation_id,sid,service_name,service_verdict,video_declared,audio_declared,video_pids_json,audio_pids_json)
          VALUES(?,?,?,?,?,?,?,?)""",
          (carrier_observation_id,sid,service_name,service_verdict,int(video_declared),int(audio_declared),
           json.dumps([int(x[0]) for x in video_pids]),json.dumps([int(x[0]) for x in audio_pids])))
        return int(cur.lastrowid)

    def persist_component(self, *, service_observation_id:int, component:str, immediate_observation:str,
                          state, pid_rows:list[tuple]) -> int:
        cur=self.connection.execute("""INSERT INTO av_component_observations(
          service_observation_id,component,immediate_observation,persistent_state,inactive_streak,recovery_streak,
          last_good_utc,last_bad_utc,last_seen_utc) VALUES(?,?,?,?,?,?,?,?,?)""",
          (service_observation_id,component,immediate_observation,state.state,state.inactive_streak,state.recovery_streak,
           state.last_good_utc,state.last_bad_utc,state.last_seen_utc))
        cid=int(cur.lastrowid)
        for pid,pstate,bitrate,delta,scrambled,stype,lang in pid_rows:
            self.connection.execute("""INSERT INTO av_pid_evidence(
              component_observation_id,pid,pid_state,bitrate,count_delta,scrambled,stream_type,language)
              VALUES(?,?,?,?,?,?,?,?)""",
              (cid,int(pid),pstate,bitrate,delta,None if scrambled is None else int(bool(scrambled)),str(stype),lang))
        return cid

    def commit(self):
        last=None
        for attempt in range(6):
            try:
                self.connection.commit(); return
            except sqlite3.OperationalError as exc:
                last=exc
                if "locked" not in str(exc).lower(): raise
                time.sleep(0.15*(attempt+1))
        raise last

    def close(self):
        self.connection.close()
