"""First-run experience works in isolation and leaves user files alone."""
import json

import pytest

from local_codebase_indexer.cli import main
from local_codebase_indexer.demo import run_demo


def test_demo_traces_indirect_access_without_touching_project(tmp_path, capsys, monkeypatch):
    # A user's optional model configuration must not be required by the bundled demo.
    monkeypatch.setenv('INDEXER_ONNX_MODEL', str(tmp_path / 'missing-model'))
    assert main(['demo', '--root', str(tmp_path), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['passed'] and all(result['checks'].values())
    assert result['index']['failed'] == 0
    assert result['sample_directory'] is None
    assert list(tmp_path.iterdir()) == []
    literal = result['literal_reference_files']
    assert 'Controllers/OrdersController.cs' not in literal
    assert 'Views/Orders/Details.cshtml' not in literal
    program = result['data_access']['programs'][0]
    assert {'Approve', 'Details'} <= set(program['actions'])
    assert 'Views/Orders/Details.cshtml' in program['pages']
    assert program['confidence'] == 'probable'


def test_retained_sample_can_be_queried_as_a_project(tmp_path, capsys):
    root = tmp_path / 'sample'
    result = run_demo(str(root))
    assert result['passed'] and result['sample_directory'] == str(root.resolve())
    assert (root / 'Sql' / 'Sales_OrderHeader.sql').is_file()
    assert main(['--root', str(root), 'data-access', 'Sales_OrderHeader', '--json']) == 0
    access = json.loads(capsys.readouterr().out)
    assert any(w['source'].endswith('OrderRepository.Approve') and w['via_chain'] for w in access['writers'])


@pytest.mark.parametrize('directory', [True, False])
def test_demo_refuses_existing_content(tmp_path, directory, capsys):
    target = tmp_path / 'keep'
    if directory:
        target.mkdir()
        original = target / 'important.txt'
    else:
        original = target
    original.write_text('keep me', encoding='utf-8')
    assert main(['demo', '--output', str(target)]) == 1
    assert 'never overwritten' in capsys.readouterr().err
    assert original.read_text(encoding='utf-8') == 'keep me'
    if directory:
        assert list(target.iterdir()) == [original]
