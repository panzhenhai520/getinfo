"""白化感知的镜像压平：多层叠加 → 单层（等价文件系统）。分两阶段，避免磁盘被撑爆。

为什么不能直接拼接各层：镜像里存在 .wh. 白化条目（后层删除了前层文件）。直接拼会把
本该被删除的文件带回来——那种镜像能启动、冒烟可能也过，但静默缺文件，最难排查。

两阶段的原因：源镜像包 7.8G、单层约 4.5G、镜像包再 4.5G，同时存在会超出可用空间。
阶段一产出单层 tar + 元数据；外层删掉源包后，阶段二再组装镜像包。

  阶段一：python3 _squash2.py phase1 <img.tar> <layer.out> <meta.json>
  阶段二：python3 _squash2.py phase2 <layer.out> <meta.json> <image.tar> <repo:tag>
"""
import hashlib
import io
import json
import os
import sys
import tarfile


def norm(name):
    return name.rstrip('/')


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def phase1(src, layer_out, meta_out):
    handle_src = tarfile.open(src)
    manifest = json.load(handle_src.extractfile('manifest.json'))
    entry = dict(manifest[0])
    config = json.load(handle_src.extractfile(entry['Config']))
    layers = list(entry['Layers'])
    print('源镜像层数 =', len(layers), flush=True)

    final = {}
    del_files = {}
    opq_dirs = {}
    for layer_index, layer_path in enumerate(layers):
        handle = handle_src.extractfile(layer_path)
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
                elif base.startswith('.wh.'):
                    target = (parent + '/' + base[4:]) if parent else base[4:]
                    del_files[target] = max(del_files.get(target, -1), layer_index)
                else:
                    final[name] = (layer_index, member_index)
        print('  扫描 %d/%d 层，索引 %d 条' % (layer_index + 1, len(layers), len(final)), flush=True)
    print('最终索引 = %d / 白化文件 = %d / 不透明目录 = %d'
          % (len(final), len(del_files), len(opq_dirs)), flush=True)

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
    with tarfile.open(layer_out, 'w') as out:
        for layer_index, layer_path in enumerate(layers):
            handle = handle_src.extractfile(layer_path)
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
    handle_src.close()
    print('写出成员数 =', written, flush=True)

    layer_digest = sha256_of(layer_out)
    config['rootfs'] = {'type': 'layers', 'diff_ids': ['sha256:' + layer_digest]}
    config['history'] = [{
        'created': config.get('created') or '2024-01-01T00:00:00Z',
        'created_by': 'flattened by tools/_squash2.py',
        'comment': 'flattened %d layers into 1 (whiteout-aware)' % len(layers),
    }]
    meta = {
        'layer_digest': layer_digest,
        'layer_size': os.path.getsize(layer_out),
        'config': config,
        'layer_count': len(layers),
        'members': written,
    }
    with open(meta_out, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False)
    print('单层 sha256 = %s  大小 = %.2f GB' % (layer_digest[:16], meta['layer_size'] / 1024 ** 3), flush=True)
    print('PHASE1_DONE', flush=True)


def phase2(layer_out, meta_out, image_out, tag):
    meta = json.load(open(meta_out, encoding='utf-8'))
    config_bytes = json.dumps(meta['config'], ensure_ascii=False).encode('utf-8')
    config_file = hashlib.sha256(config_bytes).hexdigest() + '.json'
    layer_file = meta['layer_digest'] + '/layer.tar'
    with tarfile.open(image_out, 'w') as image:
        info = tarfile.TarInfo(config_file)
        info.size = len(config_bytes)
        image.addfile(info, io.BytesIO(config_bytes))
        info = tarfile.TarInfo(layer_file)
        info.size = meta['layer_size']
        with open(layer_out, 'rb') as handle:
            image.addfile(info, handle)
        manifest_bytes = json.dumps([{
            'Config': config_file, 'RepoTags': [tag], 'Layers': [layer_file],
        }]).encode('utf-8')
        info = tarfile.TarInfo('manifest.json')
        info.size = len(manifest_bytes)
        image.addfile(info, io.BytesIO(manifest_bytes))
    print('镜像包 = %s  %.2f GB' % (image_out, os.path.getsize(image_out) / 1024 ** 3), flush=True)
    print('PHASE2_DONE', flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'phase1':
        phase1(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == 'phase2':
        phase2(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        raise SystemExit('用法: phase1 <img.tar> <layer.out> <meta.json> | phase2 <layer.out> <meta.json> <image.tar> <repo:tag>')
