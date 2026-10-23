import wechat_recv as rc
import wechat_web as w

rows = rc.query("contact.db",
                "SELECT local_type, COUNT(*) AS n FROM contact "
                "GROUP BY local_type ORDER BY n DESC")
print("local_type distribution:")
for r in rows:
    print(r)

wxid_rows = rc.query(
    "contact.db",
    "SELECT COUNT(*) AS n FROM contact "
    "WHERE username LIKE 'wxid_%' AND username NOT LIKE '%@chatroom'")
print("wxid non-room:", wxid_rows)

friends = w.list_friends("")
rooms = w.list_rooms("")
print("list_friends:", len(friends), "list_rooms:", len(rooms))

# names all wxid?
only_wxid = sum(1 for f in friends if f["name"] == f["username"])
print("friends with name==username:", only_wxid)
