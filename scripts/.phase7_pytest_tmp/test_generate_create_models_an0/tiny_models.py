from pyzzel import Base, Column, Integer, String
class Tiny(Base):
    __tablename__ = 'cli_tiny'
    id = Column(Integer, primary_key=True)
    name = Column(String)
