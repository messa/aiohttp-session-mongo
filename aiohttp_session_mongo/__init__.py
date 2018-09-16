from aiohttp_session import AbstractStorage, Session
from datetime import datetime, timedelta
import uuid

__version__ = '0.0.1'


class MongoStorage(AbstractStorage):
    def __init__(self, collection, *, cookie_name="AIOHTTP_SESSION",
                 domain=None, max_age=None, path='/',
                 secure=None, httponly=True,
                 key_factory=lambda: uuid.uuid4().hex,
                 encoder=lambda x: x, decoder=lambda x: x):
        super().__init__(cookie_name=cookie_name, domain=domain,
                         max_age=max_age, path=path, secure=secure,
                         httponly=httponly,
                         encoder=encoder, decoder=decoder)

        self._collection = collection
        self._key_factory = key_factory
        self._expire_index_created = False
        self._key_to_id_migrated = False

    async def load_session(self, request):
        await self._create_expire_index()
        await self._migrate_key_to_id()

        cookie = self.load_cookie(request)
        if cookie is None:
            return Session(None, data=None, new=True, max_age=self.max_age)
        else:
            key = str(cookie)
            stored_key = (self.cookie_name + '_' + key).encode('utf-8')
            data_row = await self._collection.find_one(
                filter={
                    '_id': stored_key,
                    '$or': [
                        {'expire': None},
                        {'expire': {'$gt': datetime.utcnow()}}
                    ]
                })

            if data_row is None:
                return Session(None, data=None,
                               new=True, max_age=self.max_age)

            try:
                data = self._decoder(data_row['data'])
            except ValueError:
                data = None
            return Session(key, data=data, new=False, max_age=self.max_age)

    async def _create_expire_index(self):
        if not self._expire_index_created:
            await self._collection.create_index([("expire", 1)],
                                                expireAfterSeconds=0)
            self._expire_index_created = True

    async def _migrate_key_to_id(self):
        from pymongo.errors import BulkWriteError
        if not self._key_to_id_migrated:
            while True:
                query = {'_id': {'$type': 'objectId'}}
                batch = await self._collection.find(query).to_list(1000)
                if not batch:
                    break
                to_insert = []
                to_delete = []
                for doc in batch:
                    new_doc = dict(doc)
                    new_doc['_id'] = doc['key']
                    del new_doc['key']
                    to_insert.append(new_doc)
                    to_delete.append(doc['_id'])
                try:
                    await self._collection.insert_many(
                        to_insert, ordered=False)
                except BulkWriteError:
                    # some ids are already present
                    pass
                await self._collection.delete_many({'_id': {'$in': to_delete}})
            self._key_to_id_migrated = True

    async def save_session(self, request, response, session):
        await self._create_expire_index()

        key = session.identity
        if key is None:
            key = self._key_factory()
            self.save_cookie(response, key,
                             max_age=session.max_age)
        else:
            if session.empty:
                self.save_cookie(response, '',
                                 max_age=session.max_age)
            else:
                key = str(key)
                self.save_cookie(response, key,
                                 max_age=session.max_age)

        data = self._encoder(self._get_session_data(session))
        expire = datetime.utcnow() + timedelta(seconds=session.max_age) \
            if session.max_age is not None else None
        stored_key = (self.cookie_name + '_' + key).encode('utf-8')
        await self._collection.update_one(
            {'_id': stored_key},
            {
                "$set": {
                    'data': data,
                    'expire': expire
                }
            },
            upsert=True)
