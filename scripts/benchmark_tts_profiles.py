"""Isolated 1x1/1x2/1x4/2x1 benchmarks; never writes project caches."""
import argparse,json,os,subprocess,sys,time
from contextlib import contextmanager
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def save(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,path)

def worker(request,dual=False):
    from model_worker import block_network
    block_network();os.chdir(request['index_root']);sys.path.insert(0,request['index_root'])
    started=time.perf_counter()
    import torch
    from indextts.infer_v2_5 import IndexTTS2
    from tts_batch import run_clips
    model=IndexTTS2(cfg_path=str(Path(request['model'])/'config.yaml'),model_dir=request['model'],use_bf16=True,use_cuda_kernel=False,use_deepspeed=False)
    loaded=time.perf_counter()-started
    base=Path(request['output']);base.mkdir(parents=True,exist_ok=True)
    warm=dict(request['clips'][0],output=str(base/'warm.wav'))
    run_clips(model,[warm],1)
    if dual:
        save(base/'ready.json',dict(model_load_seconds=loaded,reserved_mb=torch.cuda.memory_reserved()/1024**2))
        deadline=time.monotonic()+180
        while not Path(request['barrier']).is_file():
            if time.monotonic()>deadline:raise TimeoutError('benchmark barrier timed out')
            time.sleep(.1)
    results={}
    for size in ([1] if dual else [1,2,4]):
        target=base/f'batch{size}';target.mkdir(exist_ok=True)
        rows=[dict(c,output=str(target/f"{c['id']}.wav")) for c in request['clips']]
        torch.manual_seed(42);torch.cuda.reset_peak_memory_stats()
        stats=run_clips(model,rows,size,stats_path=target/'stats.json',progress_path=base/'progress.json',model_load_seconds=loaded)
        import wave
        audio=0
        for c in rows:
            with wave.open(c['output'],'rb') as f:audio+=f.getnframes()/f.getframerate()
        stats.update(audio_seconds=audio,rtf=stats['seconds']/audio,outputs=rows)
        results[str(size)]=stats;save(base/'results.json',results)
    print(json.dumps({'complete':str(base)},ensure_ascii=False),flush=True)

def gpu_memory():
    p=subprocess.run(['nvidia-smi','--query-gpu=memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    used,total=p.stdout.strip().splitlines()[0].split(',');return float(used),float(total)

def host_free_mb():
    if os.name!='nt':return 999999
    import ctypes
    class Mem(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),*[(n,ctypes.c_ulonglong) for n in ['total','available','page','available_page','virtual','available_virtual','extended']]]
    value=Mem();value.length=ctypes.sizeof(value);ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value));return value.available/1024**2

def controller(out, request):
    from filelock import FileLock
    sys.path.insert(0,str(ROOT))
    from app.config import load_settings,index_python,resolve
    settings=load_settings();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'single/results.json').exists(): raise ValueError('基准输出已存在，请指定新的 --out 目录，避免把缓存复用计作加速')
    request=Path(request).resolve()
    if not request.is_file():raise ValueError('请通过 --request 提供包含 clips 的本地 TTS 请求清单')
    original=json.loads(request.read_text(encoding='utf-8'))
    if not original.get('clips'):raise ValueError('请求清单缺少 clips')
    base=dict(index_root=str(resolve(settings.index_root)),model=str(resolve(settings.index_model)),clips=original['clips'])
    processes=[];streams=[];result=dict(profiles={},baseline_gpu_mb=gpu_memory()[0],note='Warm inference comparison; model loading recorded separately. Project caches are untouched.')
    def launch(req,flag):
        folder=Path(req['output']);folder.mkdir(parents=True,exist_ok=True);path=folder/'request.json';save(path,req)
        log=(folder/'process.log').open('wb');streams.append(log)
        p=subprocess.Popen([str(index_python(settings)),str(Path(__file__).resolve()),flag,str(path)],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'},creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(p);return p
    def wait(ps,dual=False):
        peak=0;deadline=time.monotonic()+600
        while any(p.poll() is None for p in ps):
            used,total=gpu_memory();peak=max(peak,used)
            if dual and (total-used<512 or host_free_mb()<1800):raise RuntimeError('双实例余量保护：显存不足 512 MiB 或系统可用内存不足 1800 MiB')
            if time.monotonic()>deadline:raise TimeoutError('benchmark exceeded 10 minutes')
            time.sleep(.5)
        if any(p.returncode for p in ps):raise RuntimeError('benchmark worker failed; inspect process.log')
        return peak
    @contextmanager
    def exclusive_gpu():
        with FileLock(ROOT/'runtime/gpu-model.lock',timeout=3600):
            try:yield
            finally:
                for child in processes:
                    if child.poll() is None:
                        child.terminate()
                        try:child.wait(timeout=8)
                        except subprocess.TimeoutExpired:child.kill();child.wait()
    try:
        with exclusive_gpu():
            p=launch({**base,'output':str(out/'single')},'--single');peak=wait([p])
            singles=json.loads((out/'single/results.json').read_text(encoding='utf-8'))
            for size,item in singles.items():result['profiles']['1x'+size]={**item,'gpu_total_peak_mb':peak,'gpu_peak_scope':'all single-instance profiles'}
            save(out/'benchmark.json',result);print('Single-instance profiles complete',flush=True)
            ps=[];barrier=out/'dual/go';barrier.unlink(missing_ok=True)
            for i in range(2):ps.append(launch({**base,'clips':base['clips'][i::2],'output':str(out/f'dual/{i}'),'barrier':str(barrier)},'--dual-worker'))
            deadline=time.monotonic()+180
            while not all((out/f'dual/{i}/ready.json').is_file() for i in range(2)):
                if any(p.poll() is not None for p in ps):
                    for child in ps:
                        if child.poll() is None:child.terminate();child.wait(timeout=8)
                    raise RuntimeError('双实例模型加载或预热失败')
                used,total=gpu_memory()
                if total-used<512 or host_free_mb()<1800:
                    for child in ps:
                        if child.poll() is None:child.terminate();child.wait(timeout=8)
                    raise RuntimeError('双实例加载余量保护：显存或系统内存不足')
                if time.monotonic()>deadline:raise TimeoutError('双实例模型预热超过 3 分钟')
                time.sleep(.5)
            started=time.perf_counter();barrier.parent.mkdir(parents=True,exist_ok=True);barrier.touch()
            peak=wait(ps,dual=True);seconds=time.perf_counter()-started
            workers=[json.loads((out/f'dual/{i}/results.json').read_text(encoding='utf-8'))['1'] for i in range(2)]
            audio=sum(w['audio_seconds'] for w in workers)
            result['profiles']['2x1']=dict(seconds=seconds,audio_seconds=audio,rtf=seconds/audio,gpu_total_peak_mb=peak,workers=workers,outputs=[c for w in workers for c in w['outputs']])
    except Exception as exc:
        result['dual_error']=str(exc)
        if not result['profiles']:raise
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=8)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        for s in streams:s.close()
        save(out/'benchmark.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--request',type=Path);parser.add_argument('--single',type=Path);parser.add_argument('--dual-worker',type=Path);parser.add_argument('--out',type=Path,default=ROOT/'runtime/performance-upgrade');args=parser.parse_args()
    if args.single or args.dual_worker:worker(json.loads((args.single or args.dual_worker).read_text(encoding='utf-8')),bool(args.dual_worker))
    else:
        if not args.request:parser.error('--request is required for a new benchmark')
        controller(args.out,args.request)
