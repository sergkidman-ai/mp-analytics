# поток: ret
"""Build both Windows ZIPs from reviewed source; never reads credentials or sessions."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from build_digital import build

ROOT=Path(__file__).resolve().parent


def package(source,files,destination,folder):
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            data=path.read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            info=zipfile.ZipInfo(folder+'/'+path.relative_to(source).as_posix(),date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            archive.writestr(info,data)
    return {'file':destination.name,'bytes':destination.stat().st_size,
            'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    digital=build().parent
    base_files=[ROOT/name for name in ['START.cmd','CAPTURE.cmd','EDGE.cmd','wb_qr_test.ps1','README.txt']]
    results=[package(ROOT,base_files+list((ROOT/'WB_QR_Extension').iterdir()),
                     args.output_dir/'WB_Diskver_QR_Test_Windows.zip','WB_Diskver_QR_Windows'),
             package(digital,[digital/'README.txt']+list((digital/'WB_QR_Extension').iterdir()),
                     args.output_dir/'WB_Tsifrovoy_QR_Windows.zip','WB_Tsifrovoy_QR_Windows')]
    (args.output_dir/'manifest.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))


if __name__=='__main__':
    main()
