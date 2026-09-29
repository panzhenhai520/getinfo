#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
用户数据库管理模块
用于管理用户账号、认证和权限
"""

import sqlite3
import hashlib
import secrets
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import config


def _load_dotenv_file(path='.env'):
    if not os.path.exists(path):
        return

    try:
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:].strip()
                key, value = line.split('=', 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"Warning: failed to load .env: {exc}")


_load_dotenv_file()


class UserDatabase:
    """用户数据库管理类"""
    
    def __init__(self, db_path: str = None):
        """初始化用户数据库"""
        self.db_path = (
            db_path
            or os.getenv('SQLITE_BACKUP_PATH')
            or os.getenv('DATABASE_PATH')
            or os.path.join(os.getcwd(), 'crawler_articles.db')
        )
        configured_type = str(getattr(config, 'DATABASE_TYPE', 'sqlite') or 'sqlite').strip().lower()
        self.backend = 'postgres' if configured_type in {'postgres', 'postgresql', 'pg'} else 'sqlite'
        self.connection = None
    
    def connect(self) -> bool:
        """连接数据库"""
        if self.backend == 'postgres':
            try:
                from db_connection import connect_postgres_primary
                self.connection = connect_postgres_primary()
                print(
                    f"✅ 用户数据库连接成功(PostgreSQL): "
                    f"{getattr(config, 'POSTGRES_HOST', '127.0.0.1')}:"
                    f"{getattr(config, 'POSTGRES_PORT', 5432)}/"
                    f"{getattr(config, 'POSTGRES_DB', 'collectinfo')}"
                )
                return True
            except Exception as e:
                print(f"❌ 用户数据库(PostgreSQL)连接失败: {e}")
                if not getattr(config, 'DATABASE_FALLBACK_TO_SQLITE', False):
                    return False
                print("🔄 用户数据库回退到 SQLite 备份")
                self.backend = 'sqlite'
        try:
            self.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
            self.connection.row_factory = sqlite3.Row
            print(f"✅ 用户数据库连接成功(SQLite): {self.db_path}")
            return True
        except Exception as e:
            print(f"❌ 用户数据库连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开数据库连接"""
        try:
            if self.connection:
                self.connection.close()
            label = "PostgreSQL" if self.backend == "postgres" else "SQLite"
            print(f"🔌 用户数据库连接已断开({label})")
        except Exception as e:
            print(f"❌ 断开用户数据库连接失败: {e}")
    
    def create_tables(self) -> bool:
        """创建用户相关表"""
        if self.backend == 'postgres':
            print("✅ PostgreSQL主库模式：跳过用户表 SQLite DDL")
            return True
        try:
            # 用户表
            create_users_table = """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email TEXT,
                full_name TEXT,
                role TEXT DEFAULT 'user' CHECK (role IN ('admin', 'editor', 'user')),
                is_active BOOLEAN DEFAULT TRUE,
                last_login TIMESTAMP,
                login_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
            """
            
            # 会话表
            create_sessions_table = """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_token TEXT NOT NULL UNIQUE,
                ip_address TEXT,
                user_agent TEXT,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
            
            # 操作日志表
            create_logs_table = """
            CREATE TABLE IF NOT EXISTS user_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                description TEXT,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
            
            cursor = self.connection.cursor()
            cursor.execute(create_users_table)
            cursor.execute(create_sessions_table)
            cursor.execute(create_logs_table)
            
            # 创建索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(session_token)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON user_sessions(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_user_id ON user_logs(user_id)")
            
            self.connection.commit()
            cursor.close()
            
            print("✅ 用户数据库表创建成功")
            return True
            
        except Exception as e:
            print(f"❌ 创建用户数据库表失败: {e}")
            return False
    
    def _hash_password(self, password: str) -> str:
        """密码哈希"""
        return hashlib.sha256(password.encode('utf-8')).hexdigest()
    
    def _generate_token(self) -> str:
        """生成会话令牌"""
        return secrets.token_urlsafe(32)
    
    def create_user(self, username: str, password: str, email: str = None, 
                   full_name: str = None, role: str = 'user') -> Optional[int]:
        """创建用户"""
        try:
            cursor = self.connection.cursor()
            
            # 检查用户名是否已存在
            cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
            if cursor.fetchone():
                print(f"⚠️ 用户名已存在: {username}")
                cursor.close()
                return None
            
            password_hash = self._hash_password(password)
            
            insert_sql = """
            INSERT INTO users (username, password_hash, email, full_name, role)
            VALUES (?, ?, ?, ?, ?)
            """
            
            cursor.execute(insert_sql, (username, password_hash, email, full_name, role))
            user_id = cursor.lastrowid
            self.connection.commit()
            cursor.close()
            
            print(f"✅ 用户创建成功: {username} (ID: {user_id})")
            return user_id
            
        except Exception as e:
            print(f"❌ 创建用户失败: {e}")
            return None
    
    def _ensure_connection(self):
        """确保数据库连接有效，如果断开则重新连接"""
        try:
            if self.connection is None:
                print("⚠️ 数据库连接已断开，尝试重新连接...")
                self.connect()
                self.create_tables()
            else:
                # 测试连接是否有效
                self.connection.execute("SELECT 1").fetchone()
        except Exception as e:
            print(f"⚠️ 数据库连接失效，重新连接: {e}")
            self.connect()
            self.create_tables()
    
    def verify_user(self, username: str, password: str) -> Optional[Dict]:
        """验证用户登录"""
        try:
            self._ensure_connection()
            cursor = self.connection.cursor()
            password_hash = self._hash_password(password)
            
            select_sql = """
            SELECT id, username, email, full_name, role, is_active
            FROM users 
            WHERE username = ? AND password_hash = ? AND is_active = TRUE
            """
            
            cursor.execute(select_sql, (username, password_hash))
            user = cursor.fetchone()
            
            if user:
                user_dict = dict(user)
                
                # 更新登录信息
                update_sql = """
                UPDATE users 
                SET last_login = datetime('now', 'localtime'),
                    login_count = login_count + 1,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """
                cursor.execute(update_sql, (user_dict['id'],))
                self.connection.commit()
                
                cursor.close()
                print(f"✅ 用户验证成功: {username}")
                return user_dict
            
            cursor.close()
            print(f"⚠️ 用户验证失败: {username}")
            return None
            
        except Exception as e:
            print(f"❌ 验证用户失败: {e}")
            return None
    
    def create_session(self, user_id: int, ip_address: str = None, 
                      user_agent: str = None, expire_hours: int = 24) -> Optional[str]:
        """创建会话"""
        try:
            self._ensure_connection()
            cursor = self.connection.cursor()
            token = self._generate_token()
            
            # 使用SQLite的datetime函数来确保时区一致
            insert_sql = """
            INSERT INTO user_sessions (user_id, session_token, ip_address, user_agent, expires_at, created_at)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime', '+' || ? || ' hours'), datetime('now', 'localtime'))
            """
            
            cursor.execute(insert_sql, (user_id, token, ip_address, user_agent, expire_hours))
            self.connection.commit()
            cursor.close()
            
            print(f"✅ 会话创建成功: User {user_id}, Token: {token[:10]}...")
            return token
            
        except Exception as e:
            print(f"❌ 创建会话失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def verify_session(self, token: str) -> Optional[Dict]:
        """验证会话"""
        try:
            cursor = self.connection.cursor()
            
            # 先检查会话是否存在
            cursor.execute("SELECT COUNT(*) as count FROM user_sessions WHERE session_token = ?", (token,))
            count_result = cursor.fetchone()
            if count_result and count_result['count'] == 0:
                print(f"⚠️ 会话token不存在: {token[:10]}...")
                cursor.close()
                return None
            
            # 检查会话详情
            select_sql = """
            SELECT s.*, u.username, u.email, u.full_name, u.role,
                   datetime('now', 'localtime') as current_time
            FROM user_sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.session_token = ? 
              AND s.expires_at > datetime('now', 'localtime')
              AND u.is_active = TRUE
            """
            
            cursor.execute(select_sql, (token,))
            session = cursor.fetchone()
            
            if session:
                print(f"✅ 会话验证成功: {session['username']}")
                session_dict = dict(session)
                cursor.close()
                return session_dict
            else:
                # 检查是否是因为过期
                cursor.execute("""
                    SELECT expires_at, datetime('now', 'localtime') as now
                    FROM user_sessions WHERE session_token = ?
                """, (token,))
                expiry = cursor.fetchone()
                if expiry:
                    print(f"⚠️ 会话已过期: expires_at={expiry['expires_at']}, now={expiry['now']}")
                else:
                    print(f"⚠️ 会话验证失败: token={token[:10]}...")
                cursor.close()
                return None
            
        except Exception as e:
            print(f"❌ 验证会话失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def delete_session(self, token: str) -> bool:
        """删除会话（退出登录）"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM user_sessions WHERE session_token = ?", (token,))
            self.connection.commit()
            cursor.close()
            
            print(f"✅ 会话删除成功")
            return True
            
        except Exception as e:
            print(f"❌ 删除会话失败: {e}")
            return False
    
    def get_users(self, page: int = 1, per_page: int = 20, 
                 role: str = None, is_active: bool = None) -> Tuple[List[Dict], int]:
        """获取用户列表"""
        try:
            cursor = self.connection.cursor()
            
            where_conditions = []
            params = []
            
            if role:
                where_conditions.append("role = ?")
                params.append(role)
            
            if is_active is not None:
                where_conditions.append("is_active = ?")
                params.append(is_active)
            
            where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
            
            # 获取总数
            count_sql = f"SELECT COUNT(*) as total FROM users WHERE {where_clause}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()['total']
            
            # 获取用户列表
            offset = (page - 1) * per_page
            select_sql = f"""
            SELECT id, username, email, full_name, role, is_active, 
                   last_login, login_count, created_at, updated_at
            FROM users 
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """
            params.extend([per_page, offset])
            
            cursor.execute(select_sql, params)
            users = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            
            return users, total
            
        except Exception as e:
            print(f"❌ 获取用户列表失败: {e}")
            return [], 0
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """根据ID获取用户"""
        try:
            cursor = self.connection.cursor()
            select_sql = """
            SELECT id, username, email, full_name, role, is_active, 
                   last_login, login_count, created_at, updated_at
            FROM users WHERE id = ?
            """
            cursor.execute(select_sql, (user_id,))
            user = cursor.fetchone()
            cursor.close()
            
            return dict(user) if user else None
            
        except Exception as e:
            print(f"❌ 获取用户失败: {e}")
            return None
    
    def update_user(self, user_id: int, data: Dict) -> bool:
        """更新用户信息"""
        try:
            cursor = self.connection.cursor()
            
            update_fields = []
            params = []
            
            if 'email' in data:
                update_fields.append("email = ?")
                params.append(data['email'])
            
            if 'full_name' in data:
                update_fields.append("full_name = ?")
                params.append(data['full_name'])
            
            if 'role' in data:
                update_fields.append("role = ?")
                params.append(data['role'])
            
            if 'is_active' in data:
                update_fields.append("is_active = ?")
                params.append(data['is_active'])
            
            if 'password' in data:
                update_fields.append("password_hash = ?")
                params.append(self._hash_password(data['password']))
            
            if not update_fields:
                return True
            
            update_fields.append("updated_at = datetime('now', 'localtime')")
            params.append(user_id)
            
            update_sql = f"""
            UPDATE users SET {', '.join(update_fields)}
            WHERE id = ?
            """
            
            cursor.execute(update_sql, params)
            self.connection.commit()
            cursor.close()
            
            print(f"✅ 用户更新成功: ID {user_id}")
            return True
            
        except Exception as e:
            print(f"❌ 更新用户失败: {e}")
            return False
    
    def delete_user(self, user_id: int) -> bool:
        """删除用户"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.connection.commit()
            cursor.close()
            
            print(f"✅ 用户删除成功: ID {user_id}")
            return True
            
        except Exception as e:
            print(f"❌ 删除用户失败: {e}")
            return False
    
    def log_action(self, user_id: int, action: str, description: str = None, 
                  ip_address: str = None) -> bool:
        """记录用户操作日志"""
        try:
            cursor = self.connection.cursor()
            insert_sql = """
            INSERT INTO user_logs (user_id, action, description, ip_address)
            VALUES (?, ?, ?, ?)
            """
            cursor.execute(insert_sql, (user_id, action, description, ip_address))
            self.connection.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"❌ 记录操作日志失败: {e}")
            return False
    
    def get_user_logs(self, user_id: int = None, page: int = 1, 
                     per_page: int = 50) -> Tuple[List[Dict], int]:
        """获取用户操作日志"""
        try:
            cursor = self.connection.cursor()
            
            where_clause = "WHERE user_id = ?" if user_id else "WHERE 1=1"
            params = [user_id] if user_id else []
            
            # 获取总数
            count_sql = f"SELECT COUNT(*) as total FROM user_logs {where_clause}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()['total']
            
            # 获取日志列表
            offset = (page - 1) * per_page
            select_sql = f"""
            SELECT l.*, u.username
            FROM user_logs l
            LEFT JOIN users u ON l.user_id = u.id
            {where_clause}
            ORDER BY l.created_at DESC
            LIMIT ? OFFSET ?
            """
            params.extend([per_page, offset])
            
            cursor.execute(select_sql, params)
            logs = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            
            return logs, total
            
        except Exception as e:
            print(f"❌ 获取用户日志失败: {e}")
            return [], 0
    
    def init_default_admin(self):
        """初始化默认管理员账号"""
        try:
            # 检查是否已有管理员
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM users WHERE role = 'admin'")
            admin_count = cursor.fetchone()['count']
            cursor.close()
            
            if admin_count == 0:
                admin_username = os.getenv('DEFAULT_ADMIN_USERNAME', 'admin')
                admin_password = os.getenv('DEFAULT_ADMIN_PASSWORD', 'admin123')
                admin_email = os.getenv('DEFAULT_ADMIN_EMAIL', 'admin@example.com')
                admin_full_name = os.getenv('DEFAULT_ADMIN_FULL_NAME', '系统管理员')

                # 创建默认管理员
                user_id = self.create_user(
                    username=admin_username,
                    password=admin_password,
                    email=admin_email,
                    full_name=admin_full_name,
                    role='admin'
                )
                if user_id:
                    print("✅ 默认管理员账号已创建")
                    print(f"   用户名: {admin_username}")
                    if admin_password == 'admin123':
                        print("   ⚠️ 默认密码仍为 admin123，交付前请通过 DEFAULT_ADMIN_PASSWORD 修改")
                    return True
            else:
                print(f"ℹ️  已存在 {admin_count} 个管理员账号")
            
            return True
            
        except Exception as e:
            print(f"❌ 初始化默认管理员失败: {e}")
            return False

# 全局用户数据库实例
user_db = UserDatabase()
user_db.connect()
user_db.create_tables()
user_db.init_default_admin()

