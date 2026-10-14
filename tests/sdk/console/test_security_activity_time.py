"""「最近活动」列的时间必须按本地时区显示，并且不能拿安装时间冒充活动时间。"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _run(script: str) -> None:
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_security_activity_time_renders_local_time(lang):
    """securityd 下发的是 UTC；同一列里不能出现差 8 小时的裸时间。"""
    _run(f'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
let currentLang = {lang!r};
const escapeHtml = v => String(v ?? '').replaceAll('<', '&lt;').replaceAll('&', '&amp;');
eval(source.slice(source.indexOf('function securityTimeValue('),
                  source.indexOf('function securityToolPolicy(')));
const stamp = '2026-09-23T06:18:41.302+00:00';
const noon = new Date('2026-09-23T06:18:41.302Z').getTime();
assert.equal(currentLang, {lang!r});
assert.equal(securityActivityTime(stamp, noon + 5000), currentLang === 'zh' ? '刚刚' : 'just now');
assert.equal(securityActivityTime(stamp, noon + 2 * 60 * 1000), currentLang === 'zh' ? '2 分钟前' : '2 minutes ago');
assert.equal(securityActivityTime(stamp, noon + 60 * 1000), currentLang === 'zh' ? '1 分钟前' : '1 minute ago');
assert.equal(securityActivityTime(stamp, noon + 3 * 60 * 60 * 1000), '06:18'.replace('06', '14'));
const older = '2026-09-20T06:18:41.302+00:00';
assert.equal(securityActivityTime(older, noon), '2026-09-20 ' + '14:18');
assert.equal(securityActivityTime('', noon), '');
assert.equal(securityActivityTime('not-a-time', noon), '');
assert.equal(securityTimeValue('2026-09-21 01:11:47'), securityTimeValue('2026-09-21T01:11:47Z'));
assert.equal(securityClockTime('2026-09-21 01:11:47'), '2026-09-21 09:11');
assert.equal(securityClockTime('2026-09-23T06:18:41.302+00:00'), '2026-09-23 14:18');
assert.equal(securityClockTime(''), '');
''')


def test_security_activity_cell_never_labels_install_time_as_activity():
    _run('''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
let currentLang = 'zh';
const escapeHtml = v => String(v ?? '').replaceAll('<', '&lt;').replaceAll('&', '&amp;');
eval(source.slice(source.indexOf('function securityTimeValue('),
                  source.indexOf('function securityToolPolicy(')));
const active = securityActivityCell({last_seen: '2026-09-20T06:18:41.302+00:00', installed_at: '2026-09-01T00:00:00.000+00:00'});
assert.ok(active.includes('2026-09-20 14:18'), active);
assert.ok(!active.includes('暂无活动'), active);
const quiet = securityActivityCell({last_seen: '', installed_at: '2026-09-01T00:00:00.000+00:00'});
assert.ok(quiet.includes('暂无活动'), quiet);
assert.ok(quiet.includes('安装于 2026-09-01 08:00'), quiet);
const unknown = securityActivityCell({});
assert.ok(unknown.includes('暂无活动'), unknown);
assert.ok(!unknown.includes('title='), unknown);
currentLang = 'en';
assert.ok(securityActivityCell({}).includes('No activity yet'));
''')
