"""本地模型页读不到状态时必须明说，不能静默重试。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / 'bianbu_agent/console/web'


def test_local_models_view_surfaces_gateway_failure():
    html = (WEB / 'chat.html').read_text(encoding='utf-8')
    script = (WEB / 'static/js/console.js').read_text(encoding='utf-8')
    styles = (WEB / 'static/css/console.css').read_text(encoding='utf-8')

    assert 'id="bibit-models-alert"' in html
    assert 'function renderLocalModelAlert(' in script
    assert 'renderLocalModelAlert(String((error && error.message) || error))' in script
    assert "bibit_models_service_unavailable: '本地模型状态不可用'" in script
    assert "bibit_models_service_unavailable: 'Local model state unavailable'" in script
    # 后端 502 信封是 {"error": {"code", "message"}}，必须取 message 而不是显示 HTTP 502
    assert 'envelope && (envelope.message || envelope.detail)' in script
    # 读不到状态时也读不到可用动作：页面没有下载/启动/停止按钮。
    for retired in (
        'bibit_models_download:',
        'bibit_models_cancel:',
        'bibit_models_start:',
        'bibit_models_stop:',
        'runBibitModelAction',
        'plugin-consoles',
    ):
        assert retired not in script
    assert '.bibit-model-alert' in styles
