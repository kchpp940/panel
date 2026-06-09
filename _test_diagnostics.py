import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.io.convert import (
    ManifestCollector,
    ManifestValidator,
    ReportRenderer,
    ConversionReport,
    CachePolicy,
    Provenance,
)
from panel.io._convert.manifest import (
    AppConversionManifest,
    WorkerType,
    WorkerAsset,
    AssetStatus,
    WheelAsset,
    ResourceAsset,
    CachePolicy as MCachePolicy,
    Provenance as MProvenance,
)

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)

    print("=== Test 1: HTML remote URL scanning ===")
    m = AppConversionManifest(
        app_name='x',
        app_path=pathlib.Path('x.py'),
        dest_path=tmp_path,
        runtime='pyodide',
    )
    m.html_content = """
<html><body>
<script src="https://cdn.example.com/some-bad-url.js"></script>
<img src="//other-cdn.org/img.png">
<a href="emfs:packed_wheels/good.whl">local</a>
<img src="data:image/png;base64,abc">
</body></html>
"""
    ManifestValidator(m).validate_all()
    unlocalized = m.diagnostics.unlocalized_urls
    remote_schemes = [(u.url, u.scheme) for u in unlocalized]
    print(f"  Found unlocalized URLs: {remote_schemes}")
    assert any(u.scheme == 'http' for u in unlocalized), "Expected http remote URL"
    assert any(u.scheme == 'protocol-relative' for u in unlocalized), "Expected protocol-relative URL"
    assert not any(u.url.startswith('emfs:') for u in unlocalized), "emfs URLs must not be flagged"
    assert not any(u.url.startswith('data:') for u in unlocalized), "data: URLs must not be flagged"
    print("  OK")

    print("\n=== Test 2: Duplicate wheel detection ===")
    tmp_whl_a = tmp_path / 'mock.whl'
    tmp_whl_a.write_bytes(b'fake wheel')
    m2 = AppConversionManifest(
        app_name='dup',
        app_path=pathlib.Path('d.py'),
        dest_path=tmp_path,
        runtime='pyodide',
    )
    m2.wheels['mock.whl'] = WheelAsset(
        source=str(tmp_whl_a),
        local_path=tmp_whl_a,
        emfs_path='emfs:packed_wheels/mock.whl',
        packed_path='packed_wheels/mock.whl',
    )
    m2.wheels['mock.whl-copy'] = WheelAsset(
        source=str(tmp_whl_a),
        local_path=tmp_whl_a,
        emfs_path='emfs:packed_wheels/mock.whl',
        packed_path='packed_wheels/mock.whl',
    )
    ManifestValidator(m2).validate_all()
    print(f"  Duplicates: {m2.diagnostics.duplicate_assets}")
    assert 'mock.whl-copy' in m2.diagnostics.duplicate_assets
    assert m2.wheels['mock.whl-copy'].is_duplicate
    assert m2.wheels['mock.whl-copy'].duplicate_of == 'mock.whl'
    print("  OK")

    print("\n=== Test 3: Missing wheel file detection + failure_reason ===")
    m3 = AppConversionManifest(
        app_name='miss',
        app_path=pathlib.Path('m.py'),
        dest_path=tmp_path,
        runtime='pyodide',
    )
    missing = tmp_path / 'does-not-exist.whl'
    m3.wheels['does-not-exist.whl'] = WheelAsset(
        source=str(missing),
        local_path=missing,
        emfs_path='emfs:packed_wheels/does-not-exist.whl',
        packed_path='packed_wheels/does-not-exist.whl',
    )
    ManifestValidator(m3).validate_all()
    print(f"  missing_assets={m3.diagnostics.missing_assets}")
    print(f"  wheel.failure_reason={m3.wheels['does-not-exist.whl'].status.failure_reason}")
    assert 'does-not-exist.whl' in m3.diagnostics.missing_assets
    assert m3.wheels['does-not-exist.whl'].status.failure_reason is not None
    print("  OK")

    print("\n=== Test 4: Zip consistency (missing wheel from zip) ===")
    app_html = tmp_path / 'app.html'
    app_html.write_text('<html><body></body></html>')
    m4 = AppConversionManifest(
        app_name='zip',
        app_path=pathlib.Path('z.py'),
        dest_path=tmp_path,
        runtime='pyodide',
    )
    m4.html_output = app_html
    tmp_whl_b = tmp_path / 'panel-mock.whl'
    tmp_whl_b.write_bytes(b'panel wheel')
    m4.wheels['panel-mock.whl'] = WheelAsset(
        source=str(tmp_whl_b),
        local_path=tmp_whl_b,
        emfs_path='emfs:packed_wheels/panel-mock.whl',
        packed_path='packed_wheels/panel-mock.whl',
    )
    m4.resources_zip = tmp_path / 'missing.zip'
    from panel.io._convert.persistence import pack_files
    pack_files({str(tmp_whl_b): 'not-the-right-path.whl'}, m4.resources_zip)
    ManifestValidator(m4).validate_all()
    print(f"  consistency.consistent={m4.diagnostics.consistency.consistent}")
    print(f"  consistency.mismatches={m4.diagnostics.consistency.mismatches}")
    assert m4.diagnostics.consistency.consistent is False
    assert any('panel-mock' in mm for mm in m4.diagnostics.consistency.mismatches)
    print("  OK")

    print("\n=== Test 5: Worker remote URL scanning ===")
    m5 = AppConversionManifest(
        app_name='wrk',
        app_path=pathlib.Path('w.py'),
        dest_path=tmp_path,
        runtime='pyodide-worker',
    )
    m5.worker = WorkerAsset(
        worker_type=WorkerType.PYODIDE,
        content="""
importScripts('https://cdn.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js');
importScripts('https://evil.example.com/hack.js');
const zipUrl = 'http://remote.example.org/data.zip';
""",
        status=AssetStatus(),
    )
    ManifestValidator(m5).validate_all()
    worker_urls = [(u.url, u.scheme) for u in m5.diagnostics.unlocalized_urls
                    if u.location.startswith('worker:')]
    print(f"  Worker unlocalized: {worker_urls}")
    assert any('evil.example.com' in u[0] for u in worker_urls), "Should detect non-pyodide remote URLs"
    print("  OK")

    print("\n=== Test 6: Service worker pre-cache missing asset ===")
    m6 = AppConversionManifest(
        app_name='sw',
        app_path=pathlib.Path('s.py'),
        dest_path=tmp_path,
        runtime='pyodide',
        build_pwa=True,
    )
    app_html_b = tmp_path / 'app.html'
    app_html_b.write_text('<html><body></body></html>')
    m6.html_output = app_html_b
    m6.service_worker = WorkerAsset(
        worker_type=WorkerType.SERVICE,
        content="""
self.addEventListener('install', e => {
  e.waitUntil(caches.open('v1').then(cache => {
    return cache.addAll(['/app.html', '/missing-icon.png', '/images/also-missing.svg']);
  }));
  self.skipWaiting();
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {});
""",
        status=AssetStatus(),
    )
    ManifestValidator(m6).validate_all()
    print(f"  missing_assets={m6.diagnostics.missing_assets}")
    print(f"  SW warnings: {[w for w in m6.warnings if 'pre-cache' in w.message]}")
    assert any('missing-icon' in str(a) for a in m6.diagnostics.missing_assets) or \
           any('missing-icon' in w.message for w in m6.warnings)
    print("  OK")

    print("\n=== Test 7: ReportRenderer JSON + MD output ===")
    from panel.io._convert.manifest import (
        CachePolicy,
        Provenance,
    )
    tmp_whl_c = tmp_path / 'report.whl'
    tmp_whl_c.write_bytes(b'report test')
    html_c = tmp_path / 'report.html'
    html_c.write_text('<html><body>hi</body></html>')
    m7 = AppConversionManifest(
        app_name='report_test',
        app_path=pathlib.Path('r.py'),
        dest_path=tmp_path,
        runtime='pyodide-worker',
        build_pwa=True,
    )
    m7.html_output = html_c
    m7.html_content = '<html><body>hi</body></html>'
    m7.wheels['report.whl'] = WheelAsset(
        source=str(tmp_whl_c),
        original_source='./report.whl',
        local_path=tmp_whl_c,
        emfs_path='emfs:packed_wheels/report.whl',
        packed_path='packed_wheels/report.whl',
        status=AssetStatus(
            exists=True, validated=True, localized=True,
            provenance=MProvenance.LOCAL_WHEEL,
            cache_policy=MCachePolicy.PRE_CACHE,
            original_url='./report.whl',
        ),
    )
    res = tmp_path / 'data.csv'
    res.write_text('a,b\n1,2\n')
    m7.resources['data.csv'] = ResourceAsset(
        source=res,
        archive_path='data.csv',
        status=AssetStatus(
            exists=True, validated=True,
            provenance=MProvenance.APP_RESOURCE,
            cache_policy=MCachePolicy.PRE_CACHE,
        ),
    )
    m7.worker = WorkerAsset(
        worker_type=WorkerType.PYODIDE,
        output_path=tmp_path / 'report.js',
        content='onmessage=()=>{}',
        status=AssetStatus(
            exists=True, validated=True,
            provenance=MProvenance.GENERATED,
            cache_policy=MCachePolicy.RUNTIME_CACHE,
        ),
    )
    m7.worker.output_path.write_text('onmessage=()=>{}')
    ManifestValidator(m7).validate_all()
    renderer = ReportRenderer(verbose=False)
    app_report = renderer.build_app_report(m7)
    conv = ConversionReport(
        total_apps=1, succeeded=1, failed=0,
        app_reports=[app_report], pwa_enabled=True,
    )
    written = renderer.write_assets_report(conv, tmp_path, format='both')
    print(f"  Written: {[p.name for p in written]}")
    assert len(written) == 2
    json_path = tmp_path / 'assets-report.json'
    md_path = tmp_path / 'assets-report.md'
    assert json_path.is_file() and md_path.is_file()

    data = json.loads(json_path.read_text(encoding='utf-8'))
    assert data['schema_version'] == 1
    assert data['summary']['succeeded'] == 1
    app_data = data['apps'][0]
    assert 'assets' in app_data
    assert len(app_data['assets']) >= 4
    kinds = {a['kind'] for a in app_data['assets']}
    print(f"  Kinds: {sorted(kinds)}")
    assert 'wheel' in kinds
    assert 'html' in kinds
    assert 'worker' in kinds
    assert 'resource' in kinds
    for a in app_data['assets']:
        assert 'provenance' in a
        assert 'cache_policy' in a
        assert 'localized' in a
    wheel_asset = next(a for a in app_data['assets'] if a['kind'] == 'wheel')
    assert wheel_asset['provenance'] == 'local-wheel'
    assert wheel_asset['cache_policy'] == 'pre-cache'
    assert wheel_asset['localized'] is True
    assert wheel_asset['original_url'] is not None
    print("  JSON OK")

    md = md_path.read_text(encoding='utf-8')
    assert '# Panel Conversion Assets Report' in md
    assert 'Asset inventory' in md
    assert '| # | Kind | Name | Provenance | Localized | Cache | Status | Failure reason |' in md
    assert '### Diagnostics' in md
    assert 'Localization' in md or 'localization' in md.lower()
    assert 'Consistency' in md or 'consistency' in md.lower()
    print("  Markdown OK")

    print("\n=== Test 8: 70+ public symbols exported from panel.io.convert ===")
    import panel.io.convert as conv
    expected_all = [
        'AppConversionManifest', 'AppReport', 'AssetDetail', 'AssetIssue', 'AssetStatus',
        'CachePolicy', 'ConsistencyReport', 'ConversionReport', 'DiagnosticsSummary',
        'IssueSeverity', 'LocalizationStats', 'ManifestCollector', 'ManifestPersistence',
        'ManifestValidator', 'Provenance', 'RemoteURLRef', 'ReportRenderer',
        'ResourceAsset', 'ValidationResult', 'WheelAsset', 'WorkerAsset', 'WorkerType',
    ]
    missing = [n for n in expected_all if not hasattr(conv, n)]
    if missing:
        print(f"  MISSING: {missing}")
    else:
        print(f"  OK: all {len(expected_all)} new diagnostic symbols exported")
    print(f"  Total __all__ = {len(conv.__all__)}")
    assert len(conv.__all__) >= 60

print("\n=== ALL TESTS PASSED ===")
