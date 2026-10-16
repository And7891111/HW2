import guess_game
import builtins


# 模拟输入并捕获输出（示例）：目标设为 42，输入序列 [50, 30, 42]
inputs = iter(['50', '30', '42'])

orig_input = builtins.input
orig_print = print

outputs = []


def fake_input(prompt=''):
    try:
        return next(inputs)
    except StopIteration:
        raise EOFError


def fake_print(*args, **kwargs):
    outputs.append(' '.join(str(a) for a in args))


builtins.input = fake_input
print = fake_print

# 固定随机目标为 42
import random
orig_randint = random.randint
random.randint = lambda a, b: 42

try:
    attempts = guess_game.guess_number()
finally:
    # 恢复
    builtins.input = orig_input
    random.randint = orig_randint
    print = orig_print

print('模拟运行输出行:')
for line in outputs:
    print(line)
print('返回尝试次数:', attempts)
