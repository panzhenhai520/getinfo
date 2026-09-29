"""压平前的可行性检测：统计层数与白化(.wh.)条目。

白化条目 = 某层删除了之前层的文件。没有白化时，把各层按顺序拼接成一个 tar
（后写覆盖先写）在语义上等价于原来的叠加结果，可以安全压平。
存在白化则必须先按白化语义删除，否则压平后会残留本应被删除的文件。
"""
import json
import os
import sys
import tarfile

SRC = sys.argv[1] if len(sys.argv) > 1 else '/app/data/img.tar'
with tarfile.open(SRC) as tf:
    manifest = json.load(tf.extractfile('manifest.json'))
    entry = manifest[0]
    layers = entry['Layers']
    print('镜像层数 =', len(layers))
    print('配置文件名 =', entry['Config'])
    whiteouts = []
    total_members = 0
    for lp in layers:
        fh = tf.extractfile(lp)
        if fh is None:
            continue
        with tarfile.open(fileobj=fh) as lt:
            for member in lt:
                total_members += 1
                base = os.path.basename(member.name)
                if base.startswith('.wh.'):
                    whiteouts.append((lp, member.name))
    print('成员总数 =', total_members)
    print('白化条目数 =', len(whiteouts))
    for item in whiteouts[:30]:
        print('   ', item[0], item[1])
    print('结论 =', 'SAFE_TO_CONCAT' if not whiteouts else 'NEEDS_WHITEOUT_HANDLING')
