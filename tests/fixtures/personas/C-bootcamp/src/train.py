import numpy as np

def train(x, y, lr=0.01, epochs=100):
    w = np.zeros(x.shape[1])
    for _ in range(epochs):
        w -= lr * x.T @ (x @ w - y) / len(y)
    return w
