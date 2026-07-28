import numpy as np


class RingBuffer:
    def __init__(self, length):
        self.length = length
        self.arr = None
        self.insert_idx = length - 1
        self.read_idx = 0
        self.complete = False
        self.replay_limits = (0, self.length)
        self.timepoint = np.zeros(self.length)
        self.logic = np.zeros(self.length)
        
    def put(self, item, timepoint, logic):
        try:
            if (
                self.arr is None
                or self.arr.shape[1:] != item.shape
                or self.arr.dtype != item.dtype
            ):
                self.insert_idx = 0
                self.arr = np.zeros((self.length,) + item.shape, item.dtype)

            self.arr[self.insert_idx] = item
            self.timepoint[self.insert_idx] = timepoint
            self.logic[self.insert_idx] = logic
            self.insert_idx = (self.insert_idx + 1) % self.length
            self.read_idx = 0
            if (self.insert_idx + 1) == self.length:
                self.complete = True

        except AttributeError as e:
            print(e)

    def get(self):
        if self.arr is None:
            raise ValueError("Trying to get an item from an empty buffer")
        replay_range = self.replay_limits[1] - self.replay_limits[0]
        actual_read_idx = (
            self.replay_limits[0] + self.read_idx + self.insert_idx
        ) % self.length
        out = self.arr[actual_read_idx, :, :]
        self.read_idx = (self.read_idx + 1) % replay_range
        return out

    def get_all(self):
        if self.arr is None:
            raise ValueError("Trying to get an item from an empty buffer")
        if self.complete:
            return np.concatenate([self.arr[self.insert_idx:,:,:], self.arr[:self.insert_idx,:,:]])
        return self.arr[:self.insert_idx,:,:]
            
    def get_all_meta(self):
        if self.complete:
            return np.concatenate([self.timepoint[self.insert_idx:], self.timepoint[:self.insert_idx]]), np.concatenate([self.logic[self.insert_idx:], self.logic[:self.insert_idx]])
        return self.timepoint[:self.insert_idx], self.logic[:self.insert_idx]

    def get_most_recent(self):
        return self.arr[(self.insert_idx + 1) % self.length]

    def reset(self):
        self.arr = None
        self.timepoint = np.zeros(self.length)
        self.logic = np.zeros(self.length)
        self.insert_idx = self.length - 1
        self.read_idx = 0
        self.complete = False

