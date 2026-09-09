"""Plan task-local reproduction, analysis and evidence-processing scripts."""
from pathlib import PurePosixPath


def plan(context, parameters):
    path = parameters.get('path', '')
    if (not isinstance(path, str) or not path.endswith('.py') or '\\' in path
            or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts):
        raise ValueError('custom script must be a relative .py under artifacts/custom')
    args = parameters.get('args', [])
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise ValueError('custom args must be string argv')
    timeout = int(parameters.get('timeout', 300))
    if not 1 <= timeout <= 900:
        raise ValueError('custom timeout must be between 1 and 900 seconds')
    return {'status': 'ready', 'commands': [{
        'argv': [context.get('python_bin', 'python3'),
                 context['artifact_dir'] + '/custom/' + path, *args],
        'cwd': context['source_dir'], 'env': {}, 'timeout': timeout}]}
