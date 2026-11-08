import React from "react";
import { User } from "./model";

type Props = { user: User };

export const UserCard = ({ user }: Props) => <div className="card">{user.name}</div>;

export default class UserList extends React.Component<{ users: User[] }> {
  render() {
    return <ul>{this.props.users.map((u) => <UserCard key={u.id} user={u} />)}</ul>;
  }
}
