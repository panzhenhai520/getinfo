"""白化感知的镜像压平：多层叠加 → 单层（等价文件系统）。

为什么不能直接拼接各层：镜像里存在 .wh. 白化条目（后层删除前层文件）。直接拼会把
本该被删除的文件带回来——那种镜像能启动、冒烟也可能过，但静默缺文件，最难排查。

算法：
  1) 第一遍按层顺序扫描，记录 final[path] = (层号, 成员序号)：每个路径的最后写入者；
     同时把白化事件按“目录前缀”登记（del_files 精确文件、opq_dirs 不透明目录），
     只保留最晚的一次，避免对全部路径做全量删除扫描。
  2) 第二遍按层顺序重读，只输出“本层是最后写入者、且之后没有被白化删除”的成员，
     流式写成一个单层 tar。
  3) 组装新的 config.json / manifest.json：diff_ids 用新层 sha256，history 收敛为一条。

用法：python3 squash.py <img.tar> <输出单层tar> <输出镜像包tar>
"""
import hashlib
import json
import os
import sys
import tarfile

SRC, LAYER_OUT, IMAGE_OUT = sys.argv[1], sys.argv[2], sys.argv[3]
NEW_TAG = sys.argv[4] if len(sys.argv) > 4 else 'collectinfo-web:flat'


def norm(name):
    return name.rstrip('/')


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


with tarfile.open(SRC) as tf:
    manifest = json.load(tf.extractfile('manifest.json'))
    entry = dict(manifest[0])
    config_name = entry['Config']
    config = json.load(tf.extractfile(config_name))
    layers = list(entry['Layers'])
    print('源镜像层数 =', len(layers), flush=True)

    final = {}
    del_files = {}
    opq_dirs = {}
    for layer_index, layer_path in enumerate(layers):
        handle = tf.extractfile(layer_path)
        if handle is None:
            continue
        with tarfile.open(fileobj=handle) as layer:
            for member_index, member in enumerate(layer):
                name = norm(member.name)
                if name in ('', '.'):
                    continue
                base = os.path.basename(name)
                parent = os.path.dirname(name)
                if base == '.wh..wh..opq':
                    opq_dirs[parent] = max(opq_dirs.get(parent, -1), layer_index)
                    continue
                if base.startswith('.wh.'):
                    target = (parent + '/' + base[4:]) if parent else base[4:]
                    del_files[target] = max(del_files.get(target, -1), layer_index)
                    continue
                final[name] = (layer_index, member_index)
        print('  已扫描层 %d/%d，索引 %d 条' % (layer_index + 1, len(layers), len(final)), flush=True)

    print('最终索引 =', len(final), ' 白化文件 =', len(del_files), ' 不透明目录 =', len(opq_dirs), flush=True)

    def deleted_after(name, layer_index):
        """该路径在其最后写入层之后是否被白化删除（按祖先逐级判断，O(深度)）。"""
        parts = name.split('/')
        for depth in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:depth])
            if del_files.get(prefix, -1) > layer_index:
                return True
            if opq_dirs.get(prefix, -1) > layer_index:
                return True
        return False

    written = 0
    with tarfile.open(LAYER_OUT, 'w') as out:
        for layer_index, layer_path in enumerate(layers):
            handle = tf.extractfile(layer_path)
            if handle is None:
                continue
            with tarfile.open(fileobj=handle) as layer:
                for member_index, member in enumerate(layer):
                    name = norm(member.name)
                    if name in ('', '.') or os.path.basename(name).startswith('.wh.'):
                        continue
                    if final.get(name) != (layer_index, member_index):
                        continue
                    if deleted_after(name, layer_index):
                        continue
                    payload = layer.extractfile(member) if member.isfile() else None
                    out.addfile(member, payload)
                    written += 1
        print('写出成员数 =', written, flush=True)

layer_digest = sha256_of(LAYER_OUT)
layer_size = os.path.getsize(LAYER_OUT)
print('单层 tar = %.2f GB  sha256=%s' % (layer_size / 1024 ** 3, layer_digest[:16]), flush=True)

config['rootfs'] = {'type': 'layers', 'diff_ids': ['sha256:' + layer_digest]}
config['history'] = [{
    'created': config.get('created') or '2024-01-01T00:00:00Z',
    'created_by': 'squashed by tools/_squash_image.py',
    'comment': 'flattened %d layers into 1 (whiteout-aware)' % len(layers),
}]
config_bytes = json.dumps(config, ensure_ascii=False).encode('utf-8')
config_digest = hashlib.sha256(config_bytes).hexdigest()
config_file = config_digest + '.json'
layer_file = layer_digest + '/layer.tar'

with tarfile.open(IMAGE_OUT, 'w') as image:
    import io
    info = tarfile.TarInfo(config_file)
    info.size = len(config_bytes)
    image.addfile(info, io.BytesIO(config_bytes))

    info = tarfile.TarInfo(layer_file)
    info.size = layer_size
    with open(LAYER_OUT, 'rb') as handle:
        image.addfile(info, handle)

    manifest_bytes = json.dumps([{
        'Config': config_file,
        'RepoTags': [NEW_TAG],
        'Layers': [layer_file],
    }]).encode('utf-8')
    info = tarfile.TarInfo('manifest.json')
    info.size = len(manifest_bytes)
    image.addfile(info, io.BytesIO(manifest_bytes))

print('镜像包已生成 =', IMAGE_OUT, '%.2f GB' % (os.path.getsize(IMAGE_OUT) / 1024 ** 3), flush=True)
print('SQUASH_DONE', flush=True)
