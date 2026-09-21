from __future__ import annotations
import csv, json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
MANIFEST=ROOT/'prtg_tv43_deployment'/'tv43_created_sensors.csv'
BASELINE=ROOT/'tv43_expected_services.json'

def main():
    with MANIFEST.open('r',encoding='utf-8-sig',newline='') as f:
        manifest={f"{r['host'].strip()}|M{int(r['module'])}C{int(r['channel'])}" for r in csv.DictReader(f)}
    raw=json.loads(BASELINE.read_text(encoding='utf-8'))
    carriers=raw.get('carriers')
    if not isinstance(carriers,dict): raise RuntimeError('carriers must be an object')
    configured=set(carriers)
    missing=sorted(manifest-configured); extra=sorted(configured-manifest)
    if len(manifest)!=43: raise RuntimeError(f'Expected 43 manifest carriers, found {len(manifest)}')
    if missing or extra: raise RuntimeError(f'Carrier mismatch: missing={missing} extra={extra}')
    total=0
    for key,entry in carriers.items():
        services=entry.get('services',[]) if isinstance(entry,dict) else []
        if not services: raise RuntimeError(f'No expected services for {key}')
        sids=[]
        for row in services:
            sid=int(row['sid']); name=str(row.get('name') or '').strip()
            if not name: raise RuntimeError(f'Blank service name for {key} SID {sid}')
            sids.append(sid)
        if len(sids)!=len(set(sids)): raise RuntimeError(f'Duplicate SID for {key}')
        total += len(sids)
    print(f'EXPECTED-SERVICE BASELINE: PASS | carriers={len(configured)} | services={total}')

if __name__=='__main__': main()
