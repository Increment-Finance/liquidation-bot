from dataclasses import dataclass

@dataclass
class UserPosition:
    idx: int
    user: str
    position_size: int = 0

    def AddToPosition(self, amount: int):
        self.position_size += amount

    # Define equality operator to only check idx and user, ignoring position_size
    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.idx == other.idx and self.user == other.user
        else:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)