import asyncio
import json
import time
import openai
import plugins
import os
import requests
import io
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from channel.wechat.wechat_channel import WechatChannel
from channel.wechatcom.wechatcomapp_channel import WechatComAppChannel
from channel.wechatmp.wechatmp_channel import WechatMPChannel
# from channel.wework.wework_channel import WeworkChannel
from config import conf
from plugins import *
from common.log import logger
from plugins.NetworkPlugin.lib import function as fun, get_stock_info as stock, search_google as google
from datetime import datetime
from bridge.bridge import Bridge
import config as RobotConfig
import gc
from channel import channel_factory


def create_channel_object():
    channel_type = conf().get("channel_type")
    if channel_type in ['wechat', 'wx', 'wxy']:
        return WechatChannel()
    elif channel_type == 'wechatmp':
        return WechatMPChannel()
    elif channel_type == 'wechatmp_service':
        return WechatMPChannel()
    elif channel_type == 'wechatcom_app':
        return WechatComAppChannel()
    # elif channel_type == 'wework':
    #     return WeworkChannel()
    else:
        return WechatChannel()


@plugins.register(
    name="NetworkPlugin", 
    desc="GPT的联网插件", 
    desire_priority=100, 
    version="1.0",
    author="haikerwang", )

class NetworkPlugin(Plugin):
    def __init__(self):
        super().__init__()
        
        #文件路径
        curdir = os.path.dirname(__file__)
        config_path = os.path.join(curdir, "config.json")
        functions_path = os.path.join(curdir, "lib", "functions.json")
        
        #容错
        if not os.path.exists(config_path):
            logger.info('[RP] 配置文件不存在，将使用config.json.template模板')
            config_path = os.path.join(curdir, "config.json.template")
            logger.info(f"[NetworkPlugin] config template path: {config_path}")
        
        #加载配置文件
        try:
            with open(functions_path, 'r', encoding="utf-8") as f:
                functions = json.load(f)
                self.functions = functions
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                logger.debug(f"[NetworkPlugin] config content: {config}")
                openai.api_key = conf().get("open_ai_api_key")
                openai.api_base = conf().get("open_ai_api_base", "https://api.openai.com/v1")
                self.alapi_key = config["alapi_key"]
                self.bing_subscription_key = config["bing_subscription_key"]
                self.google_api_key = config["google_api_key"]
                self.google_cx_id = config["google_cx_id"]
                self.functions_openai_model = config["functions_openai_model"]
                self.assistant_openai_model = config["assistant_openai_model"]
                self.app_key = config["app_key"]
                self.app_sign = config["app_sign"]
                self.temperature = config.get("temperature", 0.9)
                self.max_tokens = config.get("max_tokens", 1000)
                self.google_base_url = config.get("google_base_url", "https://www.googleapis.com/customsearch/v1?")
                self.comapp = create_channel_object()
                self.prompt = config["prompt"]
                self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
                logger.info("[NetworkPlugin] inited")
        except Exception as e:
            logger.error(f"初始化错误！ 错误信息：{e}")
            #错误信息
            if isinstance(e, FileNotFoundError):
                logger.warn(f"[RP] init failed, config.json not found.")
            else:
                logger.warn("[RP] init failed." + str(e))
            
            
    #处理消息
    def on_handle_context(self, e_context: EventContext):
        #非Text，默认不处理
        if e_context["context"].type not in [ContextType.TEXT]:
            return
        
        #解析内容
        context = e_context['context'].content[:]
        logger.info("NetworkPlugin query=%s" % context)
        
        #获取聊天机器人（消息上下文）
        all_sessions = Bridge().get_bot("chat").sessions
        session = all_sessions.session_query(context, e_context["context"]["session_id"])
        logger.debug("session.messages:%s" % session.messages)
        if len(session.messages) > 2:
            input_messages = session.messages[-2:]
        else:
            input_messages = session.messages[-1:]
        input_messages.append({"role": "user", "content": context})
        logger.debug("input_messages:%s" % input_messages)
        
        #回复内容
        reply_text = None
        replyType = None
        try:
            #查询是否输入的内容的联网回复，若无命中，则为None
            tpm_reply_text, tmp_replyType = self.run_conversation(input_messages, e_context)
            reply_text = tpm_reply_text
            replyType = tmp_replyType
        except Exception as e:
            logger.error(f"联网插件查询网络功能时，发生异常，错误原因：{e}，跳过处理")
            return        
        
        #回复
        if reply_text is not None and len(reply_text) > 0 and replyType is not None:
            #log
            logger.info(f"网络插件查询到内容，准备回复，内容为：{reply_text}")
            
            #回复
            context = e_context["context"]
            self.replay_use_custom(reply_text, replyType, context, e_context)
        else:
            #默认回复
            logger.info("联网插件未匹配功能模块，跳过处理")
        
        
    #使用自定义回复
    def replay_use_custom(self, reply_text: str, replyType: ReplyType, context :Context, e_context: EventContext, retry_cnt=0):
                
        try:    
            reply = Reply()
            reply.type = replyType
            reply.content = reply_text
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            # channel = e_context["channel"]
            # if channel is None:
            #     channel_name = RobotConfig.conf().get("channel_type", "wx")
            #     channel = channel_factory.create_channel(channel_name)
            #     channel.send(reply, context)
            #     #释放
            #     channel = None
            #     gc.collect() 
            # else:
            #     channel.send(reply, context)     
                
        except Exception as e:
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                self.replay_use_custom(reply_text, replyType, context, e_context, retry_cnt + 1)
                
                
    #执行功能
    def run_conversation(self, input_messages, e_context: EventContext):
        content = e_context['context'].content[:]
        logger.debug(f"用户输入: {input_messages}, 利用GPT匹配功能中...")  # 用户输入
        #利用GPT的插件能力，查询符合要求的插件名称
        response = openai.ChatCompletion.create(
            model=self.functions_openai_model,
            messages=input_messages,
            functions=self.functions,
            function_call="auto",
        )
        
        #choices
        message = response["choices"][0]["message"]
        #功能名称
        function_name = message.get("function_call").get("name")
        if function_name is None:
              return None, None
        
        #准备调用
        logger.info(f"匹配到已支持的功能，准备调用😄~；功能函数名称: {function_name}")
        
        #功能
        function_response = None
        function_responseType = ReplyType.TEXT
        # 天气
        if function_name == "get_weather":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            function_response = fun.get_weather(appkey=self.app_key, sign=self.app_sign,
                                                cityNm=function_args.get("cityNm", "未指定地点"))
            function_response = json.dumps(function_response, ensure_ascii=False)
        
        #早报
        elif function_name == "get_morning_news":
            function_response = fun.get_morning_news(api_key=self.alapi_key)
            
        #热榜
        elif function_name == "get_hotlist":
            function_args_str = message["function_call"].get("arguments", "{}")
            function_args = json.loads(function_args_str)  # 使用 json.loads 将字符串转换为字典
            hotlist_type = function_args.get("type", "未指定类型")
            function_response = fun.get_hotlist(api_key=self.alapi_key, type=hotlist_type)
            function_response = json.dumps(function_response, ensure_ascii=False)
        
        #搜索   
        elif function_name == "search":
            function_args_str = message["function_call"].get("arguments", "{}")
            function_args = json.loads(function_args_str)  # 使用 json.loads 将字符串转换为字典
            search_query = function_args.get("query", "未指定关键词")
            search_count = function_args.get("count", 1)
            if "必应" in content or "newbing" in content.lower():
                com_reply = Reply()
                com_reply.type = ReplyType.TEXT
                context = e_context['context']
                if context.kwargs.get('isgroup'):
                    msg = context.kwargs.get('msg')  # 这是WechatMessage实例
                    nickname = msg.actual_user_nickname  # 获取nickname
                    com_reply.content = "@{name}\n☑️正在给您实时联网必应搜索\n⏳整理深度数据需要时间，请耐心等待...".format(
                        name=nickname)
                else:
                    com_reply.content = "☑️正在给您实时联网必应搜索\n⏳整理深度数据需要时间，请耐心等待..."
                if self.comapp is not None:
                    self.comapp.send(com_reply, e_context['context'])
                function_response = fun.search_bing(subscription_key=self.bing_subscription_key, query=search_query,
                                                    count=int(search_count))
                function_response = json.dumps(function_response, ensure_ascii=False)
            
            elif "谷歌" in content or "搜索" in content or "google" in content.lower():
                com_reply = Reply()
                com_reply.type = ReplyType.TEXT
                context = e_context['context']
                if context.kwargs.get('isgroup'):
                    msg = context.kwargs.get('msg')  # 这是WechatMessage实例
                    nickname = msg.actual_user_nickname  # 获取nickname
                    com_reply.content = "@{name}\n☑️正在给您实时联网谷歌搜索\n⏳整理深度数据需要几分钟，请您耐心等待...".format(
                        name=nickname)
                else:
                    com_reply.content = "☑️正在给您实时联网谷歌搜索\n⏳整理深度数据需要几分钟，请您耐心等待..."
                if self.comapp is not None:
                    self.comapp.send(com_reply, e_context['context'])
                function_response = google.search_google(search_terms=search_query, base_url=self.google_base_url,
                                                            iterations=1, count=1,
                                                            api_key=self.google_api_key, cx_id=self.google_cx_id,
                                                            model=self.assistant_openai_model)
                logger.debug(f"google.search_google url: {self.google_base_url}")
                function_response = json.dumps(function_response, ensure_ascii=False)
            else:
                function_response = None
            
        #油价
        elif function_name == "get_oil_price":
            function_response = fun.get_oil_price(api_key=self.alapi_key)
        
        #星座运势查询
        elif function_name == "get_Constellation_analysis":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数

            function_response = fun.get_Constellation_analysis(api_key=self.alapi_key,
                                                                star=function_args.get("star", "未指定星座"),
                                                                )
            function_response = json.dumps(function_response, ensure_ascii=False)
        
        #音乐    
        elif function_name == "music_search":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数

            function_response = fun.music_search(api_key=self.alapi_key,
                                                 keyword=function_args.get("keyword", "未指定音乐"))
            function_response = json.dumps(function_response, ensure_ascii=False)
        
        #时间    
        elif function_name == "get_datetime":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            city = function_args.get("city_en", "未指定城市")  # 如果没有指定城市，将默认查询北京
            function_response = fun.get_datetime(appkey=self.app_key, sign=self.app_sign, city_en=city)
            function_response = json.dumps(function_response, ensure_ascii=False)
            
        #URL解析
        elif function_name == "get_url":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            url = function_args.get("url", "未指定URL")
            function_response = fun.get_url(url=url)
            function_response = json.dumps(function_response, ensure_ascii=False)
        
        #股票    
        elif function_name == "get_stock_info":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            stock_names = function_args.get("stock_names", "未指定股票信息")
            function_response = stock.get_stock_info(stock_names=stock_names, appkey=self.app_key,
                                                        sign=self.app_sign)
            function_response = json.dumps(function_response, ensure_ascii=False)
            
        #视频URL    
        elif function_name == "get_video_url":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            url = function_args.get("url", "无URL")
            viedo_url = fun.get_video_url(api_key=self.alapi_key, target_url=url)
            if viedo_url:
                logger.debug(f"viedo_url: {viedo_url}")
                function_response = viedo_url
                function_responseType = ReplyType.VIDEO_URL
            else:
                function_response = None
        
        #必应新闻
        elif function_name == "search_bing_news":
            function_args = json.loads(message["function_call"].get("arguments", "{}"))
            logger.debug(f"Function arguments: {function_args}")  # 打印函数参数
            search_query = function_args.get("query", "未指定关键词")
            search_count = function_args.get("count", 10)
            function_response = fun.search_bing_news(count=search_count,
                                                        subscription_key=self.bing_subscription_key,
                                                        query=search_query, )
            function_response = json.dumps(function_response, ensure_ascii=False)
            
        else:
            function_response = None
            logger.info("未命中联网插件的功能")
            
        #打印结果
        #logger.info(f"联网插件 - 查询的结果response: {function_response}")  
        
        #未命中，直接跳过
        if function_response is None or function_response.lower() == "null":
            logger.info("未命中联网插件的功能")
            return None, None
        
        # 非文本类型
        elif function_responseType is not ReplyType.TEXT:
            return function_response, function_responseType
            
        #处理文本 - 总结结果
        msg: ChatMessage = e_context["context"]["msg"]
        current_date = datetime.now().strftime("%Y年%m月%d日%H时%M分")
        if e_context["context"]["isgroup"]:
            prompt = self.prompt.format(time=current_date, bot_name=msg.to_user_nickname,
                                        name=msg.actual_user_nickname, content=content,
                                        function_response=function_response)
        else:
            prompt = self.prompt.format(time=current_date, bot_name=msg.to_user_nickname,
                                        name=msg.from_user_nickname, content=content,
                                        function_response=function_response)
        # log
        logger.debug(f"总结话术 prompt :" + prompt)
        logger.debug("总结话术，请求 messages: %s", [{"role": "system", "content": prompt}])
        
        #总结内容
        second_response = openai.ChatCompletion.create(
            model=self.assistant_openai_model,
            messages=[
                {"role": "system", "content": prompt},
            ],
            temperature=float(self.temperature),
            max_tokens=int(self.max_tokens)
        )
        
        #内容体
        result_content = second_response['choices'][0]['message']['content']
        logger.debug(f"总结内容体: {result_content}")
        return result_content, function_responseType

    
    #帮助说明
    def get_help_text(self, verbose=False, **kwargs):
        # 初始化帮助文本，说明利用 midjourney api 来画图
        help_text = "\n📌 功能介绍：GPT联网插件，支持网络获取目标信息\n"
        # 如果不需要详细说明，则直接返回帮助文本
        if not verbose:
            return help_text
        # 否则，添加详细的使用方法到帮助文本中
        help_text = "NetworkPlugin，GPT联网插件，前置识别\n🔎谷歌搜索、🔎新闻搜索\n🗞每日早报、☀全球天气\n⌚实时时间、⛽全国油价\n🌌星座运势、🎵音乐（网易云）\n🔥各类热榜信息、📹短视频解析等"
        # 返回帮助文本
        return help_text


