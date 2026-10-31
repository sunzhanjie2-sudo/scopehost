"""Read-only MSO44 acceptance: never send acquisition state commands."""
from pathlib import Path
import multiprocessing as mp
import runpy
import json
import os
import re
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    from scopeapp.session import ScopeSession
    from scopeapp.archive import save_archive,load_archive
    from scopeapp import __version__
    import numpy as np
    helper=runpy.run_path(str(ROOT/'tests/validate_mso44.py'))
    out=ROOT/'artifacts/mso44_v0.6.4'
    out.mkdir(parents=True,exist_ok=True)
    report={'version':__version__}
    session=ScopeSession()
    protocol,before={},{}
    writes=[]
    try:
        ad=session.connect_visa('USB0::0x0699::0x0527::C049800::INSTR')
        engine=ad.engine
        native_write=engine.transport.write
        def audited_write(command):
            assert not re.search(r'ACQ(?:UIRE)?:STATE\s+(?:RUN|STOP|[01]|ON|OFF)',command,re.I),command
            writes.append(command)
            return native_write(command)
        engine.transport.write=audited_write
        before=helper['controls'](engine)
        protocol={cmd:engine.ask(cmd+'?').strip() for cmd in (':DATA:SOURCE',':DATA:START',':DATA:STOP',':DATA:RESAMPLE',':DATA:ENCDG',':DATA:WIDTH')}
        settings={}
        def snapshot():
            settings.update(channels={f'CH{n}':ad.read_channel(n) for n in range(1,5)},timebase=ad.read_timebase(),trigger=ad.read_trigger())
            ad.update_enabled_cache(settings['channels'])
        ad.frame_settings_callback=snapshot
        start=time.monotonic()
        waveforms=ad.fetch_all()
        running=before[':ACQUIRE:STATE']=='1'
        report.update(identity=ad.identity.full,state_before=before[':ACQUIRE:STATE'],
                      elapsed_ms=(time.monotonic()-start)*1000,
                      points={name:wf.points for name,wf in waveforms.items()},
                      record_stable={name:wf.record_stable for name,wf in waveforms.items()})
        for wf in waveforms.values():
            assert wf.points==settings['timebase'].record_length==wf.raw.size
            assert abs(wf.x_incr*settings['timebase'].sample_rate-1)<1e-8
            assert wf.full_record and wf.sample_stride==1 and wf.record_stable is (not running)
        if running:
            try:
                save_archive(out/'invalid_live.npz',waveforms,settings,ad.identity.full,require_complete=True)
            except ValueError as error:
                assert '手动 STOP' in str(error)
                report['live_npz_rejected']=True
            else:
                raise AssertionError('RUN read must not be saved as a stable multichannel snapshot')
        else:
            path=out/'manual_stop_full.npz'
            save_archive(path,waveforms,settings,ad.identity.full,require_complete=True)
            loaded,_,_=load_archive(path)
            for name,wf in waveforms.items():
                assert np.array_equal(wf.raw,loaded[name].raw) and np.array_equal(wf.y,loaded[name].y)
                assert loaded[name].record_stable is True
            report['npz_all_samples_equal']=True
        after=helper['controls'](engine)
        report['controls_unchanged']=all(helper['same'](value,after[key]) for key,value in before.items())
        report['passed']=True
    finally:
        if session.connected:
            for command,value in protocol.items():
                engine.write(f'{command} {value}')
            report['events']=helper['event_status'](engine)
        session.disconnect()
        report['acquisition_state_writes']=[]
        report['commands']=writes
        filename='readonly_run.json' if before.get(':ACQUIRE:STATE')=='1' else 'manual_stop.json'
        (out/filename).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({key:value for key,value in report.items() if key!='commands'},ensure_ascii=False,indent=2),flush=True)
    return 0 if report.get('passed') and report.get('controls_unchanged') and report['events']['esr']==0 else 1

if __name__=='__main__':
    mp.freeze_support()
    sys.exit(main())
