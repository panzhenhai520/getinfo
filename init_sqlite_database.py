#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SQLite数据库初始化脚本
"""

import sys
import os
from sqlite_database import SQLiteDatabase

def init_database():
    """初始化数据库"""
    print("🚀 SQLite数据库初始化")
    print("=" * 50)
    
    # 创建数据库实例
    db = SQLiteDatabase()
    print(f"数据库路径: {db.db_path}")
    
    try:
        # 连接数据库
        if not db.connect():
            print("❌ 数据库连接失败")
            return False
        
        # 创建表
        if db.create_tables():
            print("✅ 数据库表创建成功")
            
            # 插入测试数据
            test_data = {
                'url': 'https://example.com/test-article',
                'title': '测试文章',
                'content': '这是一个测试文章的内容，用于验证SQLite数据库功能。',
                'publish_date': '2025-01-01',
                'extraction_method': 'test',
                'quality_score': 0.95
            }
            
            article_id = db.insert_article(test_data)
            if article_id:
                print(f"✅ 测试数据插入成功，文章ID: {article_id}")
                
                # 测试查询
                article = db.get_article_by_id(article_id)
                if article:
                    print(f"✅ 测试查询成功: {article['title']}")
                
                # 测试统计
                stats = db.get_statistics()
                print(f"✅ 测试统计成功: 总文章数 {stats.get('total_articles', 0)}")
                
                # 清理测试数据
                db.delete_article(article_id)
                print("✅ 测试数据清理完成")
            else:
                print("❌ 测试数据插入失败")
                return False
        else:
            print("❌ 数据库表创建失败")
            return False
        
        print("\n🎉 SQLite数据库初始化完成！")
        print("现在您可以:")
        print("1. 启动聚合应用")
        print("2. 进行聚合任务")
        print("3. 文章将自动入库到SQLite")
        print("4. 通过API访问文章数据")
        print(f"5. 数据库文件位置: {db.db_path}")
        
        return True
        
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        return False
    finally:
        db.disconnect()

def test_connection():
    """测试数据库连接"""
    print("🔍 测试SQLite数据库连接")
    print("=" * 50)
    
    db = SQLiteDatabase()
    
    if db.connect():
        print("✅ 数据库连接成功")
        print(f"数据库文件: {db.db_path}")
        db.disconnect()
        return True
    else:
        print("❌ 数据库连接失败")
        return False


def migrate_database():
    """只执行数据库结构迁移，不写入测试文章"""
    print("🔧 执行SQLite数据库结构迁移")
    print("=" * 50)

    db = SQLiteDatabase()
    print(f"数据库路径: {db.db_path}")

    try:
        if not db.connect():
            print("❌ 数据库连接失败")
            return False
        if db.create_tables():
            print("✅ 数据库结构迁移完成")
            return True
        print("❌ 数据库结构迁移失败")
        return False
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        return False
    finally:
        db.disconnect()

def show_info():
    """显示数据库信息"""
    print("📊 SQLite数据库信息")
    print("=" * 50)
    
    db = SQLiteDatabase()
    print(f"数据库路径: {db.db_path}")
    print(f"文件存在: {os.path.exists(db.db_path)}")
    
    if os.path.exists(db.db_path):
        file_size = os.path.getsize(db.db_path)
        print(f"文件大小: {file_size} 字节 ({file_size / (1024*1024):.2f} MB)")
    
    if db.connect():
        cursor = None
        try:
            # 获取表信息
            cursor = db.connection.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            print(f"数据表数量: {len(tables)}")
            for table in tables:
                print(f"  - {table['name']}")

            cursor.execute("PRAGMA table_info(articles)")
            article_columns = [row['name'] for row in cursor.fetchall()]
            print(f"articles.source_task_id列: {'存在' if 'source_task_id' in article_columns else '缺失'}")
            print(f"articles.source_task_name列: {'存在' if 'source_task_name' in article_columns else '缺失'}")
            
            # 获取文章数量
            cursor.execute("SELECT COUNT(*) as count FROM articles WHERE status='active'")
            article_count = cursor.fetchone()['count']
            print(f"文章数量: {article_count}")
            
        except Exception as e:
            print(f"获取信息失败: {e}")
        finally:
            if cursor:
                cursor.close()
            db.disconnect()

def main():
    """主函数"""
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == 'test':
            test_connection()
        elif command == 'init':
            init_database()
        elif command == 'migrate':
            migrate_database()
        elif command == 'info':
            show_info()
        elif command == 'help':
            print("SQLite数据库管理工具")
            print("=" * 50)
            print("用法: python init_sqlite_database.py [命令]")
            print("\n可用命令:")
            print("  init  - 初始化数据库（创建表和索引）")
            print("  migrate - 执行结构迁移（补齐新增字段，不插入测试数据）")
            print("  test  - 测试数据库连接")
            print("  info  - 显示数据库信息")
            print("  help  - 显示此帮助信息")
            print("\n示例:")
            print("  python init_sqlite_database.py init")
            print("  python init_sqlite_database.py migrate")
            print("  python init_sqlite_database.py test")
            print("  python init_sqlite_database.py info")
        else:
            print("未知命令，使用 'python init_sqlite_database.py help' 查看帮助")
    else:
        # 默认执行初始化
        init_database()

if __name__ == "__main__":
    main()
