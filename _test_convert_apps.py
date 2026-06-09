import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.io.convert import convert_app, convert_apps

app_a = """
import panel as pn
slider = pn.widgets.FloatSlider(start=0, end=10)
pn.Row(slider, pn.bind(lambda v: v, slider.param.value)).servable();
"""

app_b = """
import panel as pn
button = pn.widgets.Button()
pn.Row(button, pn.bind(lambda c: c, button.param.clicks)).servable();
"""

if __name__ == '__main__':
    sys.modules['_pyodide'] = sys.modules['json']  # fake pyodide to force single-process path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)

        # Test 1: convert_app backward-compatible return value (tuple of 2)
        print("=== Test 1: convert_app return compatibility ===")
        f_a = tmp_path / 'app_a.py'
        f_a.write_text(app_a, encoding='utf-8')
        result = convert_app(
            str(f_a), str(tmp_path), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False, verbose=False,
            generate_assets_report=True,
        )
        assert result is not None, f"convert_app returned None"
        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2, f"Expected 2-tuple, got {len(result)}-tuple: {result}"
        name, filename = result
        assert isinstance(name, str) and isinstance(filename, str)
        print(f"  OK: convert_app -> ({name!r}, {filename!r})")
        html_path = tmp_path / filename
        assert html_path.is_file(), f"HTML not written: {html_path}"
        json_path = tmp_path / 'assets-report.json'
        assert json_path.is_file(), f"Single-app assets-report.json missing"
        data = json.loads(json_path.read_text())
        assert len(data['apps']) == 1
        assert len(data['apps'][0]['assets']) >= 1, \
            f"Expected >=1 asset, got {len(data['apps'][0]['assets'])}: {data['apps'][0]['assets']}"
        kinds = {a['kind'] for a in data['apps'][0]['assets']}
        assert 'html' in kinds, f"Missing 'html' in asset kinds: {kinds}"
        for asset in data['apps'][0]['assets']:
            assert 'provenance' in asset, f"Asset {asset} missing provenance"
            assert 'cache_policy' in asset, f"Asset {asset} missing cache_policy"
            assert 'localized' in asset, f"Asset {asset} missing localized"
        print(f"  OK: single-app report has {len(data['apps'][0]['assets'])} assets, kinds={sorted(kinds)}")

        # Test 2: convert_app _return_report=True returns 3-tuple
        print("\n=== Test 2: convert_app _return_report=True returns 3-tuple ===")
        result3 = convert_app(
            str(f_a), str(tmp_path), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False, verbose=False,
            generate_assets_report=False, _return_report=True,
        )
        assert isinstance(result3, tuple) and len(result3) == 3, \
            f"Expected 3-tuple, got {result3!r}"
        name3, filename3, report3 = result3
        assert report3 is not None, "AppReport should not be None"
        print(f"  OK: 3-tuple report.app_name={report3.app_name}, success={report3.success}")

        # Test 3: convert_apps with 2 apps + PWA
        print("\n=== Test 3: convert_apps multi-app PWA ===")
        dest_pwa = tmp_path / 'pwa_dest'
        dest_pwa.mkdir()
        f1 = dest_pwa / 'slider_app.py'
        f1.write_text(app_a, encoding='utf-8')
        f2 = dest_pwa / 'button_app.py'
        f2.write_text(app_b, encoding='utf-8')

        convert_apps(
            [str(f1), str(f2)], str(dest_pwa), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False,
            build_pwa=True, build_index=True, verbose=False,
            requirements={str(f1): 'auto', str(f2): 'auto'},
        )
        assert (dest_pwa / 'slider_app.html').is_file(), "slider_app.html missing"
        assert (dest_pwa / 'button_app.html').is_file(), "button_app.html missing"
        assert (dest_pwa / 'index.html').is_file(), "index.html missing"
        assert (dest_pwa / 'site.webmanifest').is_file(), "site.webmanifest missing"
        assert (dest_pwa / 'serviceWorker.js').is_file(), "serviceWorker.js missing"
        assert (dest_pwa / 'images').is_dir(), "PWA images dir missing"
        print("  OK: all disk outputs present (2 html + index + manifest + sw + images)")

        json_path = dest_pwa / 'assets-report.json'
        md_path = dest_pwa / 'assets-report.md'
        assert json_path.is_file(), f"Multi-app assets-report.json missing"
        assert md_path.is_file(), f"Multi-app assets-report.md missing"
        data = json.loads(json_path.read_text())
        assert data['summary']['total_apps'] == 2, f"Expected 2 total apps: {data['summary']}"
        assert len(data['apps']) >= 2, f"Expected >=2 app reports, got {len(data['apps'])}"
        app_names = {a['name'] for a in data['apps']}
        assert 'slider_app' in app_names and 'button_app' in app_names, \
            f"Missing app names in report: {app_names}"
        for a in data['apps']:
            assert len(a['assets']) >= 1, f"App {a['name']} has <1 asset"
            assert 'diagnostics' in a, f"App {a['name']} missing diagnostics"
            for asset in a['assets']:
                assert 'provenance' in asset, f"Asset {asset} missing provenance"
                assert 'cache_policy' in asset, f"Asset {asset} missing cache_policy"
                assert 'localized' in asset, f"Asset {asset} missing localized"
        print(f"  OK: multi-app report has {len(data['apps'])} apps with detailed diagnostics")
        for a in data['apps']:
            print(f"    - {a['name']}: {len(a['assets'])} assets, diagnostics keys={list(a['diagnostics'].keys())}")

        md = md_path.read_text()
        assert 'slider_app' in md and 'button_app' in md
        assert 'Asset inventory' in md
        print("  OK: markdown includes both apps and asset inventory")

        # Test 4: convert_apps WITHOUT PWA still writes assets report
        print("\n=== Test 4: convert_apps build_pwa=False still writes assets report ===")
        dest_nopwa = tmp_path / 'nopwa_dest'
        dest_nopwa.mkdir()
        f3 = dest_nopwa / 'slider_app.py'
        f3.write_text(app_a, encoding='utf-8')
        convert_apps(
            [str(f3)], str(dest_nopwa), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False,
            build_pwa=False, build_index=False, verbose=False,
        )
        assert (dest_nopwa / 'slider_app.html').is_file(), "HTML missing"
        json_path = dest_nopwa / 'assets-report.json'
        assert json_path.is_file(), f"Non-PWA assets-report.json missing"
        data2 = json.loads(json_path.read_text())
        assert data2['summary']['pwa_enabled'] is False
        assert len(data2['apps']) >= 1
        print(f"  OK: non-PWA report has {len(data2['apps'])} app(s), pwa_enabled=False")

        # Test 5: convert_apps requirements as dict with path endswith matching
        print("\n=== Test 5: convert_apps requirements dict path matching ===")
        dest_req = tmp_path / 'req_dest'
        dest_req.mkdir()
        fa = dest_req / 'slider_app.py'
        fa.write_text(app_a, encoding='utf-8')
        fb = dest_req / 'button_app.py'
        fb.write_text(app_b, encoding='utf-8')
        convert_apps(
            [str(fa), str(fb)], str(dest_req), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False,
            build_pwa=False, build_index=False, verbose=False,
            requirements={'slider_app.py': 'auto', 'button_app.py': 'auto'},
        )
        assert (dest_req / 'slider_app.html').is_file()
        assert (dest_req / 'button_app.html').is_file()
        data3 = json.loads((dest_req / 'assets-report.json').read_text())
        assert data3['summary']['succeeded'] >= 2
        print(f"  OK: dict requirements matched both apps, succeeded={data3['summary']['succeeded']}")

        # Test 6: generate_assets_report=False should NOT write assets report
        print("\n=== Test 6: convert_apps generate_assets_report=False skips report ===")
        dest_no = tmp_path / 'no_report_dest'
        dest_no.mkdir()
        fx = dest_no / 'slider_app.py'
        fx.write_text(app_a, encoding='utf-8')
        convert_apps(
            [str(fx)], str(dest_no), runtime='pyodide',
            prerender=False, panel_version='auto', inline=False,
            build_pwa=False, build_index=False, verbose=True,
            generate_assets_report=False,
        )
        assert (dest_no / 'slider_app.html').is_file(), "HTML still written"
        assert not (dest_no / 'assets-report.json').is_file(), "JSON report should NOT exist"
        assert not (dest_no / 'assets-report.md').is_file(), "MD report should NOT exist"
        print("  OK: generate_assets_report=False suppresses report writing")

    print("\n=== ALL INTEGRATION TESTS PASSED ===")
