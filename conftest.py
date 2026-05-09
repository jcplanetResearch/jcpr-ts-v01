"""
프로젝트 루트 conftest — pytest가 src/ 패키지를 찾을 수 있도록 sys.path 보정.
이 파일이 있는 디렉터리가 자동으로 sys.path[0]에 추가됨.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
