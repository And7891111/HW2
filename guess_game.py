import random


def guess_number() -> int:
    """运行数字猜谜游戏：
    - 生成闭区间 [1, 100] 的随机整数目标
    - 循环读取用户输入（stdin），要求为 1..100 的整数；无效输入提示并重试
    - 每次合法猜测后打印提示："太大"、"太小" 或在猜中时打印 "正确" 并返回尝试次数（int）
    """
    target = random.randint(1, 100)
    attempts = 0

    while True:
        try:
            user_input = input().strip()

            if not user_input:
                print('请输入1到100之间的整数')
                continue

            guess = int(user_input)
            if guess < 1 or guess > 100:
                print('请输入1到100之间的整数')
                continue

            attempts += 1

            if guess > target:
                print('太大')
            elif guess < target:
                print('太小')
            else:
                # 精确匹配：使用验收标准要求的文本
                print('正确')
                return attempts

        except ValueError:
            print('请输入1到100之间的整数')
            continue


def main():
    attempts = guess_number()
    # 可选：在交互结束后显示尝试次数（不作为验收标准的一部分，按需保留或注释）
    # print(f"共猜测次数: {attempts}")


if __name__ == '__main__':
    main()
