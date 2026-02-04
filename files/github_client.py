#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub API客户端
Author: Lingma
Description: 封装GitHub API操作，包含网络重试、缓存和性能优化
"""

import requests
import json
import base64
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import time
from functools import wraps
import hashlib

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def cache_result(expiry_seconds=300):
    """缓存装饰器"""
    def decorator(func):
        cache = {}
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            # 生成缓存键
            key = hashlib.md5(str(args).encode() + str(kwargs).encode()).hexdigest()
            
            # 检查缓存
            if key in cache:
                result, timestamp = cache[key]
                if datetime.now() - timestamp < timedelta(seconds=expiry_seconds):
                    logger.debug(f"缓存命中: {func.__name__}")
                    return result
            
            # 执行原始函数
            result = func(self, *args, **kwargs)
            
            # 缓存结果
            if result is not None:
                cache[key] = (result, datetime.now())
                logger.debug(f"缓存更新: {func.__name__}")
            
            return result
        return wrapper
    return decorator

class GitHubClient:
    """GitHub API客户端 - 优化版（包含缓存、批量操作和性能优化）"""
    
    def __init__(self, token: str, username: str):
        self.token = token
        self.username = username
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # 缓存相关
        self.cache = {}
        self.cache_expiry = 300  # 5分钟缓存
        
        # 创建带重试机制的session
        self.session = requests.Session()
        self._setup_session()
        
        # 请求统计
        self.request_count = 0
        self.cached_requests = 0
        
        logger.info(f"GitHub客户端初始化 - 用户: {username}")
    
    def _setup_session(self):
        """配置带重试机制的HTTP session"""
        # 配置重试策略
        retry_strategy = Retry(
            total=3,  # 总重试次数
            backoff_factor=1,  # 重试间隔倍数
            status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的状态码
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"]
        )
        
        # 配置适配器
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        
        # 应用到http和https
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # 设置默认headers
        self.session.headers.update(self.headers)
    
    def _make_request(self, method: str, url: str, use_cache: bool = True, **kwargs) -> Optional[requests.Response]:
        """统一的请求方法，包含重试、缓存和错误处理"""
        # 缓存键生成
        cache_key = None
        if use_cache and method.upper() == 'GET':
            cache_key = hashlib.md5((url + str(kwargs)).encode()).hexdigest()
            
            # 检查缓存
            if cache_key in self.cache:
                cached_result, timestamp = self.cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.cache_expiry):
                    self.cached_requests += 1
                    logger.debug(f"缓存命中: {url}")
                    return cached_result
        
        # 设置默认超时
        if 'timeout' not in kwargs:
            kwargs['timeout'] = (10, 30)  # (连接超时, 读取超时)
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.request_count += 1
                logger.debug(f"发起请求 [{method}] {url} (尝试 {attempt + 1}/{max_retries})")
                response = self.session.request(method, url, **kwargs)
                
                # 缓存GET请求的成功响应
                if cache_key and response.status_code < 400:
                    self.cache[cache_key] = (response, datetime.now())
                
                # 检查状态码
                if response.status_code < 400:
                    return response
                elif response.status_code == 401:
                    logger.error("认证失败: Token无效或过期")
                    return response
                elif response.status_code == 403:
                    logger.error("权限不足: 请检查Token权限")
                    return response
                elif response.status_code in [429, 500, 502, 503, 504]:
                    # 对于这些状态码进行重试
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + 1  # 指数退避
                        logger.warning(f"请求失败 (状态码: {response.status_code})，{wait_time}秒后重试...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"重试{max_retries}次后仍然失败: {response.status_code}")
                        return response
                else:
                    # 其他错误状态码直接返回
                    return response
                    
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + 1
                    logger.warning(f"请求超时，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error("请求超时，已达最大重试次数")
                    return None
            except requests.exceptions.ConnectionError as e:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + 1
                    logger.warning(f"连接错误: {str(e)}, {wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"连接错误，已达最大重试次数: {str(e)}")
                    return None
            except Exception as e:
                logger.error(f"请求异常: {str(e)}")
                return None
        
        return None
    
    @cache_result(300)  # 5分钟缓存
    def get_user_repos(self) -> Optional[List[Dict]]:
        """获取用户所有仓库 - 带缓存"""
        try:
            url = f"{self.base_url}/user/repos?per_page=100"
            logger.info(f"获取用户仓库列表: {url}")
            
            response = self._make_request("GET", url, use_cache=True)
            
            if response is None:
                logger.error("网络请求失败")
                return None
                
            if response.status_code == 200:
                repos = response.json()
                logger.info(f"成功获取 {len(repos)} 个仓库")
                return repos
            else:
                logger.error(f"获取仓库列表失败: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"获取仓库列表时发生未知错误: {str(e)}")
            return None
    
    def get_repo_by_name(self, name: str) -> Optional[Dict]:
        """根据名称获取特定仓库信息 - 带缓存"""
        cache_key = f"repo_{name}"
        if cache_key in self.cache:
            result, timestamp = self.cache[cache_key]
            if datetime.now() - timestamp < timedelta(seconds=self.cache_expiry):
                return result
        
        try:
            url = f"{self.base_url}/repos/{self.username}/{name}"
            response = self._make_request("GET", url, use_cache=False)  # 不使用全局缓存
            if response and response.status_code == 200:
                result = response.json()
                self.cache[cache_key] = (result, datetime.now())  # 单独缓存
                return result
            return None
        except:
            return None
    
    def create_repo(self, name: str, private: bool = False) -> Optional[Dict]:
        """创建仓库"""
        try:
            url = f"{self.base_url}/user/repos"
            data = {
                "name": name,
                "private": private,
                "auto_init": True
            }
            logger.info(f"创建仓库: {name}")
            
            response = self._make_request("POST", url, use_cache=False, json=data)
            
            if response is None:
                return None
                
            if response.status_code == 201:
                repo_info = response.json()
                logger.info(f"仓库创建成功: {repo_info.get('full_name')}")
                # 清除相关缓存
                self._clear_repo_cache()
                return repo_info
            elif response.status_code == 422:
                # 仓库可能已存在
                logger.warning(f"仓库 {name} 可能已存在")
                return self._get_existing_repo(name)
            else:
                logger.error(f"创建仓库失败: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"创建仓库时发生错误: {str(e)}")
            return None
    
    def _get_existing_repo(self, name: str) -> Optional[Dict]:
        """获取已存在的仓库"""
        return self.get_repo_by_name(name)
    
    def _clear_repo_cache(self):
        """清除仓库相关的缓存"""
        keys_to_remove = [k for k in self.cache.keys() if k.startswith('repo_')]
        for key in keys_to_remove:
            del self.cache[key]
        logger.debug("已清除仓库缓存")
    
    def batch_upload_files(self, repo: str, files: List[Tuple[str, bytes]], 
                          message: str = "Batch upload") -> List[Dict]:
        """批量上传文件 - 减少API调用"""
        results = []
        for path, content in files:
            result = self.upload_file(repo, path, content, message)
            results.append({
                'path': path,
                'success': result is not None,
                'result': result
            })
        return results
    
    def upload_file(self, repo: str, path: str, content: bytes, 
                   message: str = "Upload file") -> Optional[Dict]:
        """上传文件到仓库"""
        try:
            url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
            encoded_content = base64.b64encode(content).decode('utf-8')
            
            data = {
                "message": message,
                "content": encoded_content
            }
            
            logger.info(f"上传文件到: {repo}/{path}")
            logger.info(f"文件大小: {len(content)} 字节")
            
            response = self._make_request("PUT", url, use_cache=False, json=data)
            
            if response is None:
                return None
                
            if response.status_code == 201:
                result = response.json()
                logger.info(f"文件上传成功: {path}")
                return result
            elif response.status_code == 422:
                # 文件可能已存在，尝试更新
                logger.warning(f"文件 {path} 已存在，尝试更新...")
                return self._update_existing_file(repo, path, content, message)
            else:
                logger.error(f"上传文件失败: {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"上传文件时发生错误: {str(e)}")
            return None
    
    def _update_existing_file(self, repo: str, path: str, content: bytes, 
                             message: str) -> Optional[Dict]:
        """更新已存在的文件"""
        try:
            # 先获取文件信息获取SHA
            get_url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
            get_response = self._make_request("GET", get_url, use_cache=False)
            
            if get_response and get_response.status_code == 200:
                file_info = get_response.json()
                sha = file_info['sha']
                
                # 更新文件
                put_url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
                encoded_content = base64.b64encode(content).decode('utf-8')
                
                data = {
                    "message": message,
                    "content": encoded_content,
                    "sha": sha
                }
                
                put_response = self._make_request("PUT", put_url, use_cache=False, json=data)
                
                if put_response and put_response.status_code == 200:
                    logger.info(f"文件更新成功: {path}")
                    return put_response.json()
            
            logger.error("更新文件失败")
            return None
            
        except Exception as e:
            logger.error(f"更新文件时发生错误: {str(e)}")
            return None
    
    @cache_result(180)  # 3分钟缓存
    def list_files(self, repo: str, path: str = "") -> List[Dict]:
        """列出仓库中的文件 - 带缓存"""
        try:
            # 获取仓库信息以确定默认分支
            repo_info = self.get_repo_by_name(repo)
            if not repo_info:
                logger.error(f"无法获取仓库信息: {repo}")
                return []

            default_branch = repo_info.get('default_branch', 'main')
            url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}?ref={default_branch}"
            logger.info(f"列出文件: {repo}/{path} (分支: {default_branch})")
            
            response = self._make_request("GET", url, use_cache=True)
            
            if response is None:
                return []
                
            if response.status_code == 200:
                files = response.json()
                logger.info(f"成功获取 {len(files)} 个项目")
                return files
            else:
                logger.warning(f"列出文件返回状态码: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"列出文件时发生错误: {str(e)}")
            return []
    
    def download_file(self, repo: str, path: str) -> Optional[bytes]:
        """从仓库下载文件 - 增强版"""
        try:
            # 首先获取文件信息
            list_url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
            logger.info(f"获取文件信息: {repo}/{path}")
            
            response = self._make_request("GET", list_url, use_cache=True)
            
            if response is None:
                logger.error("获取文件信息网络请求失败")
                return None
                
            if response.status_code != 200:
                logger.error(f"获取文件信息失败: {response.status_code}")
                return None
            
            file_info = response.json()
            
            # 如果是文件，直接下载内容
            if file_info.get('type') == 'file':
                # 优先使用download_url
                download_url = file_info.get('download_url')
                if download_url:
                    logger.info(f"使用download_url下载: {download_url}")
                    return self._download_from_raw_url(download_url)
                else:
                    # 如果没有download_url，使用content字段（base64编码）
                    content_encoded = file_info.get('content')
                    if content_encoded:
                        content = base64.b64decode(content_encoded)
                        logger.info(f"文件解码成功: {len(content)} 字节")
                        return content
                    else:
                        logger.error("文件内容为空")
                        return None
            else:
                logger.error("路径不是文件")
                return None
                
        except Exception as e:
            logger.error(f"下载文件时发生错误: {str(e)}")
            return None
    
    def _download_from_raw_url(self, url: str) -> Optional[bytes]:
        """从raw URL下载文件，带重试机制"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"开始下载文件 (尝试 {attempt + 1}/{max_retries})")
                
                # 使用session进行下载，带重试
                response = self.session.get(url, timeout=(10, 60))
                
                if response.status_code == 200:
                    content = response.content
                    logger.info(f"文件下载成功: {len(content)} 字节")
                    return content
                elif response.status_code in [429, 500, 502, 503, 504]:
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + 2
                        logger.warning(f"下载失败 (状态码: {response.status_code})，{wait_time}秒后重试...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"下载重试{max_retries}次后仍然失败: {response.status_code}")
                        return None
                else:
                    logger.error(f"下载失败: {response.status_code}")
                    return None
                    
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + 2
                    logger.warning(f"下载超时，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error("下载超时，已达最大重试次数")
                    return None
            except Exception as e:
                logger.error(f"下载异常: {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + 1
                    logger.warning(f"下载异常，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    return None
        
        return None
    
    def delete_file(self, repo: str, path: str, sha: str, 
                   message: str = "Delete file") -> bool:
        """删除仓库中的文件"""
        try:
            url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
            data = {
                "message": message,
                "sha": sha
            }
            logger.info(f"删除文件: {repo}/{path}")
            
            response = self._make_request("DELETE", url, use_cache=False, json=data)
            
            if response is None:
                return False
                
            if response.status_code == 200:
                logger.info(f"文件删除成功: {path}")
                return True
            else:
                logger.error(f"删除文件失败: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"删除文件时发生错误: {str(e)}")
            return False
    
    def get_file_info(self, repo: str, path: str) -> Optional[Dict]:
        """获取单个文件信息"""
        try:
            url = f"{self.base_url}/repos/{self.username}/{repo}/contents/{path}"
            logger.info(f"获取文件信息: {repo}/{path}")
            
            response = self._make_request("GET", url, use_cache=True)
            
            if response is None:
                logger.error("获取文件信息网络请求失败")
                return None
                
            if response.status_code == 200:
                file_info = response.json()
                if file_info.get('type') == 'file':
                    logger.info(f"成功获取文件信息: {file_info.get('name')}")
                    return file_info
                else:
                    logger.error("路径不是文件")
                    return None
            elif response.status_code == 404:
                logger.error("文件不存在")
                return None
            else:
                logger.error(f"获取文件信息失败: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"获取文件信息时发生错误: {str(e)}")
            return None
    
    def enable_lfs(self, repo: str) -> bool:
        """启用Git LFS"""
        try:
            lfs_patterns = [
                "*.zip filter=lfs diff=lfs merge=lfs -text",
                "*.rar filter=lfs diff=lfs merge=lfs -text",
                "*.7z filter=lfs diff=lfs merge=lfs -text",
                "*.mp4 filter=lfs diff=lfs merge=lfs -text",
                "*.mov filter=lfs diff=lfs merge=lfs -text"
            ]
            
            content = "\n".join(lfs_patterns).encode('utf-8')
            result = self.upload_file(repo, ".gitattributes", content, 
                                    "Enable Git LFS for large files")
            
            if result:
                logger.info("Git LFS启用成功")
                return True
            else:
                logger.error("Git LFS启用失败")
                return False
                
        except Exception as e:
            logger.error(f"启用Git LFS时发生错误: {str(e)}")
            return False
    
    def get_api_usage_stats(self) -> Dict:
        """获取API使用统计"""
        return {
            'total_requests': self.request_count,
            'cached_requests': self.cached_requests,
            'cache_hit_rate': self.cached_requests / self.request_count if self.request_count > 0 else 0
        }
    
    def clear_cache(self):
        """清空所有缓存"""
        self.cache.clear()
        logger.info("缓存已清空")
    
    def close(self):
        """关闭session"""
        self.session.close()
        logger.info("GitHub客户端已关闭")