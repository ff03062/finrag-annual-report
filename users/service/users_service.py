import os
import secrets
import smtplib
from email.mime.text import MIMEText

from common import redisConn
from users.dao.check_email import check_email

from users.entity.users_entity import CheckCodeEntity

CODE_TTL_SECONDS = 60


def send_email(email):
    users = check_email(email)
    # 查不到用户时 check_email 返回的是空列表，不能只判断 isinstance(list)
    if not users:
        return {
            'code': 500,
            'msg': '邮箱不存在',
            'data': None
        }

    # 用 secrets 生成验证码（random 是可预测的伪随机数）
    code = ''.join(secrets.choice('0123456789') for _ in range(4))

    # 现在把验证码存入到redis中
    r = redisConn.get_redis_conn()
    try:
        r.setex(email, CODE_TTL_SECONDS, code)
    finally:
        redisConn.close_redis_connection(r)

    # 配置发送信息
    sender = os.getenv("SENDER_EMAIL")  # 发送方
    sender_pwd = os.getenv("SENDER_EMAIL_PASSWORD")  # 发送方授权码
    subject = '来自[克劳瑞德的deepseek]，你注册的登陆验证码是...：'
    content = f'你的登陆验证码是：{code}\n请{CODE_TTL_SECONDS}秒内使用，切勿泄露给他人'
    message = MIMEText(content, 'plain', 'utf-8')
    message['From'] = sender
    message['To'] = email
    message['Subject'] = subject

    smtp = None
    try:
        smtp = smtplib.SMTP(
            host=os.getenv("SMTP_HOST"),
            port=int(os.getenv("SMTP_PORT")),
            timeout=10,  # 不设超时的话 SMTP 卡住会把工作线程一直占着
        )
        # 开启邮箱发送服务TLS
        smtp.starttls()
        # 验证发生方授权码是否对的上
        smtp.login(sender, sender_pwd)
        # 发送
        smtp.sendmail(sender, email, message.as_string())

        return {
            "code": 200,
            "msg": "验证码已发送",
            "data": users[0]['username']  # ← 返回数据库里的用户名
        }
    except Exception as e:
        print(f"发送失败:{e}")
        return {
            'code': 500,
            "msg": "发送失败",
            "data": None
        }
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


def check_code(checkCodeEntity: CheckCodeEntity):
    email = checkCodeEntity.email
    code = checkCodeEntity.code

    # 先确认用户存在，否则后面 result[0] 会 IndexError 变成 500
    users = check_email(email)
    if not users:
        return {
            "code": 500,
            "msg": "邮箱不存在",
            "data": None,
        }

    r = redisConn.get_redis_conn()
    try:
        redis_code = r.get(email)
        if redis_code is None:
            return {
                "code": 500,
                "msg": "验证码已过期",
                "data": None,
            }
        # 进行验证码的比对
        if redis_code != code:
            return {
                "code": 500,
                "msg": "验证码不正确",
                "data": None,
            }
        # 验证码一次性使用，校验通过立即失效，避免 60 秒内被重复使用
        r.delete(email)
    finally:
        redisConn.close_redis_connection(r)

    from users.utils.JWTUtil import create_access_token
    token = create_access_token({
        "user_id": users[0]["user_id"],
        "username": users[0]["username"],
    })
    return {
        "code": 200,
        "msg": "验证码正确",
        "data": {"token": token}
    }


if __name__ == '__main__':
    email = '2069985674@qq.com'
    send_email(email)
    code = input('请输入验证码：')
    test_entity = CheckCodeEntity(email=email, code=code)
    check_code(test_entity)
