import asyncio
import json
from types import SimpleNamespace

import pytest

import backend.audio as audio
import backend.engine as engine_module
from backend.config import Settings
from backend.engine import Engine


def test_device_scan_uses_fresh_process_and_short_shared_cache(monkeypatch):
    devices = [{'id': 7, 'name': 'Gaming', 'default': True, 'channels': 8, 'rate': 96000}]
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps(devices).encode())

    monkeypatch.setattr(audio.subprocess, 'run', run)
    monkeypatch.setattr(audio, '_devices_cache', (0, []))
    assert audio.list_devices() == devices
    audio.list_devices()[0]['name'] = 'must not mutate cache'
    assert audio.list_devices() == devices
    assert len(calls) == 1
    assert calls[0][0][-2:] == ['-m', 'backend.audio']
    monkeypatch.setattr(audio, '_devices_cache', (0, []))
    devices[0].update(name='Chat', channels=2, rate=48000)
    assert audio.list_devices()[0]['name'] == 'Chat'
    assert len(calls) == 2


@pytest.fixture
def fake_sources(monkeypatch):
    devices = [dict(id=7, name='Gaming', default=True, channels=8, rate=96000),
               dict(id=8, name='Chat', default=False, channels=2, rate=48000)]
    captures = []

    class FakeCapture:
        def __init__(self, *callbacks):
            self.stopped = False
            captures.append(self)

        def start(self, device_id, *args):
            device = next(d for d in devices if d['default']) if device_id is None else next(d for d in devices if d['id'] == device_id)
            self.device_info = dict(device)
            self.gain = args[-1]
            return device['name']

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(engine_module, 'Capture', FakeCapture)
    monkeypatch.setattr(engine_module, 'list_devices', lambda: [dict(d) for d in devices])
    return devices, captures


def settings():
    return Settings(llm_model='test', llm_api_key='test', asr_api_key='test', audio_gain=4)


@pytest.mark.asyncio
async def test_manual_source_switch_keeps_history_pairing_asr_and_answer(fake_sources):
    devices, captures = fake_sources
    engine = Engine(settings(), None)
    engine.items.append({'question': '已有问题', 'answer': '已有回答'})
    token = engine.controller_token
    try:
        await engine.start(None)
        epoch, asr = engine.epoch, engine.asr_task
        answer = engine.answer_task = asyncio.create_task(asyncio.sleep(100))
        await engine.switch_source(8)
        assert captures[0].stopped and captures[1].gain == 4
        assert engine.state['device'] == 'Chat'
        assert engine.state['device_info']['channels'] == 2
        assert engine.state['follow_default'] is False
        assert engine.epoch == epoch and engine.asr_task is asr
        assert not answer.done() and engine.answer_task is answer
        assert len(engine.items) == 1 and engine.controller_token == token
    finally:
        await engine.stop()
    assert engine.device_task is None and captures[-1].stopped


@pytest.mark.asyncio
async def test_default_output_changes_reopen_capture_while_fixed_source_stays(fake_sources):
    devices, captures = fake_sources
    following = Engine(settings(), None)
    fixed = Engine(settings(), None)
    try:
        await following.start(None)
        await fixed.start(7)
        devices[0]['default'], devices[1]['default'] = False, True
        async with asyncio.timeout(4):
            while following.state['device'] != 'Chat':
                await asyncio.sleep(0.05)
        assert following.state['follow_default'] is True
        assert fixed.state['device'] == 'Gaming'
        assert len(captures) == 3
        assert not following.asr_task.done() and not fixed.asr_task.done()
    finally:
        await following.stop()
        await fixed.stop()


@pytest.mark.asyncio
async def test_watch_failure_surfaces_and_stop_cancels_monitor(fake_sources, monkeypatch):
    engine = Engine(settings(), None)
    def broken_scan():
        raise RuntimeError('设备扫描失败')
    monkeypatch.setattr(engine_module, 'list_devices', broken_scan)
    try:
        await engine.start(None)
        async with asyncio.timeout(4):
            while not engine.state['audio_notice']:
                await asyncio.sleep(0.05)
        assert engine.state['listening'] and '设备扫描失败' in engine.state['audio_notice']
    finally:
        await engine.stop()
    assert engine.device_task is None
