"""Application release contents, including data required by on-host tests."""
from pathlib import Path
import tarfile

DIRECTORIES = ('orion', 'scripts', 'config', 'tests', 'examples', 'docs')
FILES = ('pyproject.toml', 'requirements.txt', 'bootstrap/github-recovery-permissions.json')


def build_archive(archive, *, root=Path('.')):
    with tarfile.open(archive, 'w:gz') as tar:
        for directory in DIRECTORIES:
            for path in sorted((root/directory).rglob('*')):
                if '__pycache__' in path.parts:
                    continue
                if path.is_symlink():
                    raise ValueError('Release cannot contain symlinks')
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
        for name in FILES:
            path = root/name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Required release file missing or linked: '+name)
            tar.add(path, arcname=name, recursive=False)
