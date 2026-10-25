# поток: ret
"""Build the Digital Windows extension from the tested Diskver source, without secrets."""
from pathlib import Path
import json

SOURCE=Path(__file__).resolve().parent/'WB_QR_Extension'
TARGET=SOURCE.parent.parent/'wb_buyer_windows_digital'/'WB_QR_Extension'

def build():
    TARGET.mkdir(parents=True,exist_ok=True)
    replacements=[('Дисквэра','Цифрового'),('Дисквэру','Цифровому'),('Дисквэр','Цифровой'),('Дисквер','Цифровой'),('WB_Diskver','WB_Tsifrovoy'),('diskver','tsifrovoy')]
    for source in SOURCE.iterdir():
        if not source.is_file():continue
        data=source.read_bytes()
        if source.name not in ('jsQR.js','jsQR-LICENSE.txt'):
            text=data.decode()
            for old,new in replacements:text=text.replace(old,new)
            text=text.replace('/returns/wb-qr/setup"','/returns/wb-qr/setup/tsifrovoy"')
            data=text.encode()
        (TARGET/source.name).write_bytes(data)
    manifest=json.loads((TARGET/'manifest.json').read_text())
    manifest['name']='QR WB — Цифровой'
    (TARGET/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return TARGET

if __name__=='__main__':
    print('Digital extension built:',len(list(build().iterdir())),'files')
