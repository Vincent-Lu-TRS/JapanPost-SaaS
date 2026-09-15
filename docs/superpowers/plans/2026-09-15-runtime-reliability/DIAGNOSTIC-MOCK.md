# 現有缺陷的隔離重現

這不是修復實作，也不是Linux／正式製單驗收。2026-09-15 10:39:20 JST主對話重新執行下列腳本，exit0；Python3.14.6／Streamlit1.56.0。

在repo根目錄的PowerShell執行。所有下載、解包、mkdir、write_text及browser installer均為Mock；只清除這段AST建立的隔離cache，不清正式App cache、不啟動app或郵局瀏覽器。

```powershell
@'
import ast, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from urllib.error import HTTPError
import streamlit as st
from bot import playwright_runtime as runtime

attempts = []
def fake_downloader(package, destination):
    attempts.append(package.name)
    if len(attempts) == 1:
        raise HTTPError(package.url, 503, 'mock unavailable', None, None)

kwargs = dict(env={}, runtime_root='mock-runtime-no-files', platform_name='Linux',
              machine_name='x86_64', library_checker=lambda _: False,
              downloader=fake_downloader, extractor=lambda *_: None,
              apply_to_process_env=False)
with patch.object(runtime, '_vendor_libraries_ready', return_value=False), \
     patch.object(runtime, '_vendor_library_files_present', return_value=True), \
     patch.object(Path, 'mkdir'), patch.object(Path, 'write_text'):
    first = runtime.prepare_playwright_runtime(**kwargs)
    second = runtime.prepare_playwright_runtime(**kwargs)
assert not first.ok and second.ok and len(attempts) == 62
print('helper_first_ok=', first.ok, 'message=', first.message)
print('helper_second_ok=', second.ok, 'download_calls=', len(attempts))

app_ast = ast.parse(Path('app.py').read_text(encoding='utf-8-sig'))
installer_ast = next(node for node in app_ast.body
    if isinstance(node, ast.FunctionDef) and node.name == '_install_playwright')
prepare_mock = Mock(side_effect=[
    SimpleNamespace(ok=False, message='mock HTTPError', env={}),
    SimpleNamespace(ok=True, message='mock ready', env={}),
])
run_mock = Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
ns = {'__name__': 'isolated_runtime_audit_main', 'st': st, 'sys': sys,
      'subprocess': SimpleNamespace(run=run_mock),
      'prepare_playwright_runtime': prepare_mock}
exec(compile(ast.Module(body=[installer_ast], type_ignores=[]), 'app.py', 'exec'), ns)
installer = ns['_install_playwright']
a, b = installer(), installer()
assert (a, b, prepare_mock.call_count, run_mock.call_count) == (False, False, 1, 0)
print('streamlit_version=', st.__version__, 'cached_calls=', (a, b),
      'prepare_calls=', prepare_mock.call_count, 'browser_install_calls=', run_mock.call_count)
installer.clear()
c = installer()
assert c is True and prepare_mock.call_count == 2 and run_mock.call_count == 1
print('after_isolated_cache_clear=', c, 'prepare_calls=', prepare_mock.call_count,
      'browser_install_calls=', run_mock.call_count)
'@ | & 'C:\Python314\python.exe' -B -
```

實際主要輸出：

```text
helper_first_ok= False message= runtime library preparation failed: HTTPError
helper_second_ok= True download_calls= 62
streamlit_version= 1.56.0 cached_calls= (False, False) prepare_calls= 1 browser_install_calls= 0
after_isolated_cache_clear= True prepare_calls= 2 browser_install_calls= 1
```

隔離模式同時出現Streamlit的missing ScriptRunContext警告；assertion仍全部通過。503是本機故障注入值，**不是今日Cloud日誌已證實的HTTP狀態**。實作後應改用計畫T1目標測試（失敗不快取、可復原），不能繼續以「False,False」作成功標準。
